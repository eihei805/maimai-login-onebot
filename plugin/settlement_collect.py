"""Read-only snapshot collection with an injected minimal Client and Session.

No runtime imports, login, retries, persistence or networking at import time.
The caller holds its account lock throughout collection AND submission. Client
must expose _current(session) and _game(api, body, user_agent_identity), as in
maimai-minimal-client.py. Tests inject an entirely offline implementation.

Contracts: Project-Upsert/api/user_getter_common.py documents userId-only
character/detail requests, music paging (50), item paging (100, kind * 1e10),
and favorite itemKind. Artemis base.py and AquaDX Maimai2Apis.kt establish
userRating/userActivity wrappers; buddiesplus.py returns weekly + mission in
GetUserMissionDataApi. Character is NOT a paged request. Unknown continuation
on a singleton is rejected rather than inventing request fields.

Unresolved variants fail closed: character nextAwake/nextAwakePercent/point
conversion, missing awakening/useCount, missing userData.banState, alternate
favorite wrappers, missing weekly/mission fields, and unknown save fields.
Caller must supply both extend flags; no required save data is synthesized.
"""
from copy import deepcopy

from basic_settlement import required
from settlement_snapshot import normalize_snapshot

ITEM_FACTOR = 10_000_000_000
ITEM_KINDS = (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 14)
FAVORITE_KINDS = (3, 1, 2, 10, 11)


class CollectionError(ValueError):
    """Incomplete or unsupported snapshot; never submit a partial result."""


def _need(ok, message):
    if not ok:
        raise CollectionError(message)


def collect_snapshot(client, session, *, captured_at, extend_flags,
                     item_kinds=(1,), page_budget=128, row_budget=100000):
    """Return the exact normalized baseline consumed by build_basic_settlement.

    captured_at is an explicit real Unix timestamp taken by the locked caller
    BEFORE collection. Freshness/order is subsequently checked by the builder.
    Budgets are per paged scope, with a shared row budget for the full snapshot.
    Transport exceptions propagate; there are no automatic retries.
    """
    _need(type(page_budget) is int and 1 <= page_budget <= 1024, 'invalid page_budget')
    _need(type(row_budget) is int and 1 <= row_budget <= 1000000, 'invalid row_budget')
    _need(type(captured_at) is int and captured_at > 0, 'invalid captured_at')
    _need(type(item_kinds) in (tuple, list) and bool(item_kinds), 'invalid item_kinds')
    _need(all(type(k) is int and k in ITEM_KINDS for k in item_kinds), 'invalid item kind')
    _need(len(set(item_kinds)) == len(item_kinds), 'duplicate item scope')
    flags = {k: required(extend_flags, k, bool) for k in ('isPhotoAgree', 'isGotoCodeRead')}
    binding = tuple(getattr(session, k, None) for k in
                    ('user_id', 'login_id', 'login_date_time', 'user_agent_identity'))
    uid, login_id, login_time, identity = binding
    _need(all(type(v) is int and v > 0 for v in binding[:3]), 'invalid session binding')
    _need(type(identity) is str, 'invalid session identity')
    remaining = row_budget

    def current():
        client._current(session)
        _need(binding == tuple(getattr(session, k, None) for k in
              ('user_id', 'login_id', 'login_date_time', 'user_agent_identity')),
              'session binding changed')

    def read(api, fields=None, *, paged=False, account_echo=True):
        current()
        reply = client._game(api, dict(userId=uid, **(fields or {})), identity)
        current()
        _need(type(reply) is dict, api + ': expected response object')
        if 'returnCode' in reply:
            _need(type(reply['returnCode']) is int and reply['returnCode'] == 1,
                  api + ': unsuccessful returnCode')
        if account_echo or 'userId' in reply:
            _need(type(reply.get('userId')) is int and reply['userId'] == uid,
                  api + ': account mismatch or missing userId')
        if not paged and 'nextIndex' in reply:
            _need(type(reply['nextIndex']) is int and reply['nextIndex'] == 0,
                  api + ': unresolved singleton continuation')
        return deepcopy(reply)

    def rows(value, label):
        nonlocal remaining
        _need(type(value) is list, label + ': missing or invalid array')
        remaining -= len(value)
        _need(remaining >= 0, 'snapshot row budget exhausted')
        _need(all(type(r) is dict for r in value), label + ': expected object rows')
        for row in value:
            if 'userId' in row:
                _need(type(row['userId']) is int and row['userId'] == uid,
                      label + ': nested account mismatch')
        return value

    def unique(values, keys, label):
        seen = set()
        for row in values:
            key = tuple(required(row, k, int) for k in keys)
            _need(key not in seen, label + ': duplicate identity')
            seen.add(key)
        return values

    def pages(api, key, *, kind=None):
        cursor = 0 if kind is None else kind * ITEM_FACTOR
        visited, result = set(), []
        for _ in range(page_budget):
            _need(cursor not in visited, api + ': cursor cycle')
            visited.add(cursor)
            reply = read(api, dict(nextIndex=cursor, maxCount=50 if kind is None else 100), paged=True)
            nxt = required(reply, 'nextIndex', int)
            _need(0 <= nxt <= 2**63-1, api + ': invalid cursor')
            page = rows(reply.get(key), key)
            if kind is None:
                flattened = []
                for group in page:
                    flattened.extend(rows(group.get('userMusicDetailList'), 'userMusicDetailList'))
                page = flattened
            else:
                _need(type(reply.get('itemKind')) is int and reply['itemKind'] == kind,
                      api + ': response kind mismatch')
                _need(all(type(r.get('itemKind')) is int and r['itemKind'] == kind for r in page),
                      api + ': row kind mismatch')
            result.extend(page)
            if nxt == 0:
                return unique(result, ('musicId', 'level') if kind is None else ('itemKind', 'itemId'), key)
            _need(bool(page), api + ': empty continuation')
            _need(nxt not in visited, api + ': cursor cycle')
            if kind is not None:
                _need(nxt // ITEM_FACTOR == kind, api + ': cursor kind mismatch')
            cursor = nxt
        raise CollectionError(api + ': page budget exhausted')

    raw = {'userData': read('GetUserDataApi')}
    for api, source, target in (
        ('GetUserExtendApi', 'userExtend', 'userExtend'),
        ('GetUserOptionApi', 'userOption', 'userOption'),
        ('GetUserRatingApi', 'userRating', 'userRatingList')):
        raw[target] = required(read(api), source, dict)
    mission = read('GetUserMissionDataApi')
    raw['userWeeklyData'] = required(mission, 'userWeeklyData', dict)
    if 'userId' in raw['userWeeklyData']:
        _need(type(raw['userWeeklyData']['userId']) is int and raw['userWeeklyData']['userId'] == uid,
              'userWeeklyData: nested account mismatch')
    raw['userMissionDataList'] = rows(mission.get('userMissionDataList'), 'userMissionDataList')
    charge = read('GetUserChargeApi')
    raw['userChargeList'] = rows(charge.get('userChargeList'), 'userChargeList')
    if 'length' in charge:
        _need(type(charge['length']) is int and charge['length'] == len(raw['userChargeList']),
              'userChargeList: length mismatch')
    # Activity replies in both reference servers omit userId. Binding comes
    # from the current session and explicit request; reject any conflicting echo.
    activity = required(read('GetUserActivityApi', account_echo=False), 'userActivity', dict)
    for key in ('playList', 'musicList'):
        rows(activity.get(key), 'userActivity.' + key)
    raw['userActivityList'] = [activity]
    raw['userFavoriteList'] = []
    for kind in FAVORITE_KINDS:
        favorite = required(read('GetUserFavoriteApi', dict(itemKind=kind)), 'userFavorite', dict)
        ids = required(favorite, 'itemIdList', list)
        remaining -= len(ids)
        _need(remaining >= 0, 'snapshot row budget exhausted')
        raw['userFavoriteList'].append(dict(requestedKind=kind, response=favorite))
    characters = read('GetUserCharacterApi')
    raw['userCharacterList'] = unique(rows(characters.get('userCharacterList'), 'userCharacterList'),
                                      ('characterId',), 'userCharacterList')
    raw['userMusicDetailList'] = pages('GetUserMusicApi', 'userMusicList')
    raw['userItemList'] = []
    for kind in item_kinds:
        raw['userItemList'].extend(pages('GetUserItemApi', 'userItemList', kind=kind))
    current()
    return normalize_snapshot(raw, user_id=uid, login_id=login_id,
        login_date_time=login_time, captured_at=captured_at, complete=True,
        item_kinds=list(item_kinds), extend_flags=flags)
