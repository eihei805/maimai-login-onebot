"""Experimental independent score and cosmetic deltas without gameplay.

The native ExportUserAll evidence uses BuildListData for independent music/item
struct deltas. Its normal exporter ALWAYS emits a game playlog, however: schema
acceptance of empty playlogs is not proof that a native server accepts this
no-play aggregate edit. No live acceptance is claimed. ``verified`` means the
requested rows and preserved inventories were read back, not rating recalculation.
Caller holds the account lock from fresh collection through return. Claims are
shared with settlement_submit and consumed permanently before the single write.
"""
from copy import deepcopy
import math
import time

from basic_settlement import SCHEMA, check, required
from game_score_upload import NODES, _bits, _bounded, _index, _readback as _score_readback
from item_upload import KINDS, add_collections, item_delta, validate_items
from settlement_collect import ITEM_KINDS
from settlement_submit import _claim

# Native MusicClearrankID order: D,C,B,BB,BBB,A,AA,AAA,S,S+,SS,SS+,SSS,SSS+.
# Assembly GamePlayManager.GetClearRank (0x06002E96) compares achievement to
# the next rank's MusicClearRankData.achieve, stopping at enum value 13.
# These are the standard chart thresholds; special utage styles are unsupported.
RANK_THRESHOLDS = (500000, 600000, 700000, 750000, 800000, 900000, 940000,
                   970000, 980000, 990000, 995000, 1000000, 1005000)


class _NotImproved(ValueError):
    pass


def _baseline(snapshot, machine, binding, auth_date_time, clock):
    if type(clock) not in (int, float) or not math.isfinite(clock):
        raise ValueError('clock')
    offset = required(machine, 'utcOffsetSeconds', int)
    if not -50400 <= offset <= 50400:
        raise ValueError('offset')
    if type(auth_date_time) is not int or not 0 < auth_date_time <= clock + offset:
        raise ValueError('auth_time')
    captured = required(snapshot, 'capturedAt', int)
    if not 0 <= clock - captured <= 300 or captured < binding[2] - offset:
        raise ValueError('stale_snapshot')
    if required(snapshot, 'complete', bool) is not True:
        raise ValueError('incomplete_snapshot')
    for key, value in zip(('userId', 'loginId', 'loginDateTime'), binding):
        if required(snapshot, key, int) != value:
            raise ValueError('snapshot_binding')
    scopes = required(snapshot, 'itemKinds', list)
    if not scopes or any(type(k) is not int or k not in ITEM_KINDS for k in scopes) or len(set(scopes)) != len(scopes):
        raise ValueError('inventory_scopes')
    base = required(snapshot, 'nodes', dict)
    if set(base) != NODES:
        raise ValueError('snapshot_shape')
    for name, rows in base.items():
        check(rows, SCHEMA['UserAll'][name], name)
    for name in ('userData', 'userExtend', 'userOption', 'userRatingList'):
        if len(base[name]) != 1:
            raise ValueError('singleton')
    validate_items(base['userItemList'])
    if any(r['itemKind'] not in scopes for r in base['userItemList']):
        raise ValueError('inventory_scope_mismatch')
    for name, keys in (('userMusicDetailList', ('musicId', 'level')),
                       ('userChargeList', ('chargeId',)), ('userCharacterList', ('characterId',)),
                       ('userFavoriteList', ('itemKind',))):
        _index(base[name], keys)
    if any(base['userData'][0][k] < 0 for k in ('playCount', 'currentPlayCount')):
        raise ValueError('negative_playcount')
    raw = required(snapshot, 'rawFavoriteKinds', list)
    favorites = base['userFavoriteList']
    if len(raw) != len(favorites) or {r['itemKind'] for r in favorites} != set(KINDS.values()):
        raise ValueError('favorite_scopes')
    for kind, row in zip(raw, favorites):
        if kind is None:
            if row['itemIdList']:
                raise ValueError('null_favorite_contents')
        elif type(kind) is not int or kind != row['itemKind']:
            raise ValueError('favorite_binding')
    return base


def build_typed_settlement(snapshot, machine, *, user_id, login_id, login_date_time,
                           auth_date_time, catalog, operation, now_unix):
    """Build a full envelope with an independent score or cosmetic delta.

    operation uses numeric protocol musicId (DX namespace included) and level
    0..4, or a cosmetic kind integer / exact KINDS name. Scalar achievements use
    10000 units per percent, e.g. 1010000 = 101%. All other best fields and counts
    are preserved for scores; new chart fields explicitly start at zero. Score
    operations do not depend on song 8 or rating compatibility. Cosmetic changes
    use only item deltas, preserve play counts and emit no gameplay logs;
    no tickets are consumed.
    auth time is validated but not written into a score operation's profile.
    """
    for value in (user_id, login_id, login_date_time):
        if type(value) is not int or value <= 0:
            raise ValueError('session_binding')
    _bounded(snapshot)
    _bounded(operation)
    base = deepcopy(_baseline(snapshot, machine, (user_id, login_id, login_date_time),
                              auth_date_time, now_unix))
    ua = {key: '' if typ == 'str' else [] for key, typ in SCHEMA['UserAll'].items()}
    for name in ('userData', 'userExtend', 'userOption', 'userRatingList', 'userWeeklyData',
                 'userChargeList', 'userFavoriteList', 'userActivityList', 'userMissionDataList'):
        ua[name] = base[name]
    ua['isNewFavoriteList'] = ''.join('1' if k is None else '2' for k in snapshot['rawFavoriteKinds'])
    ua['user2pPlaylog'] = dict(userId1=0, userId2=0, userName1='', userName2='',
                             regionId=0, placeId=0, user2pPlaylogDetailList=[])
    mode = required(operation, 'operation', str)
    if mode == 'score':
        if set(operation) != {'operation', 'music_id', 'level', 'achievement', 'title'}:
            raise ValueError('score_shape')
        mid, level, achievement = (required(operation, k, int) for k in ('music_id', 'level', 'achievement'))
        title = required(operation, 'title', str)
        if not 0 < mid <= 2147483647 or not 0 <= level <= 4 or not 0 <= achievement <= 1010000:
            raise ValueError('score_range')
        if type(catalog) is not list:
            raise ValueError('catalog')
        matches = [r for r in catalog if type(r) is dict and str(r.get('id')) == str(mid)]
        if (len(matches) != 1 or not title or matches[0].get('title') != title
                or matches[0].get('type') not in ('SD', 'DX') or mid >= 100000):
            raise ValueError('catalog_identity')
        charts = required(matches[0], 'charts', list)
        if level >= len(charts) or type(charts[level]) is not dict:
            raise ValueError('catalog_chart')
        old = _index(base['userMusicDetailList'], ('musicId', 'level')).get((mid, level))
        if old is not None and achievement <= old['achievement']:
            raise _NotImproved('score_not_improved')
        detail = deepcopy(old) if old else dict(musicId=mid, level=level, playCount=0,
            achievement=0, comboStatus=0, syncStatus=0, deluxscoreMax=0, scoreRank=0, extNum1=0)
        detail.update(achievement=achievement, scoreRank=sum(achievement >= t for t in RANK_THRESHOLDS))
        ua['userMusicDetailList'] = [detail]
        ua['isNewMusicDetailList'] = '0' if old else '1'
    elif mode == 'item':
        if set(operation) != {'operation', 'kind', 'item_id'}:
            raise ValueError('item_shape')
        kind = operation['kind']
        if type(kind) is str:
            kind = KINDS.get(kind)
        if type(kind) is not int or kind not in KINDS.values() or kind not in snapshot['itemKinds']:
            raise ValueError('item_scope')
        item_id = required(operation, 'item_id', int)
        delta = item_delta(base['userItemList'], add_collections(base['userItemList'], kind, [item_id]))
        ua.update(delta)
    else:
        raise ValueError('operation')
    check(ua, 'UserAll', 'upsertUserAll')
    _bits(ua)
    return dict(userId=user_id, playlogId=login_id, loginDateTime=login_date_time,
                isEventMode=required(machine, 'isEventMode', bool),
                isFreePlay=required(machine, 'isFreePlay', bool), userPlaylogList=[], upsertUserAll=ua)


def _readback(client, session, packet, snapshot, current):
    checks = _score_readback(client, session, packet, current)
    # An item edit has no chart assertion. Omit that check instead of claiming
    # success on an empty chart list (or on a failed unrelated music GET).
    if not packet['upsertUserAll']['userMusicDetailList']:
        checks.pop('charts')
    checks['items'] = False
    try:
        actual = {}
        for kind in snapshot['itemKinds']:
            cursor, seen = kind * 10_000_000_000, set()
            for _ in range(128):
                if cursor in seen:
                    raise ValueError('item_cycle')
                seen.add(cursor)
                current()
                reply = client._game('GetUserItemApi', dict(userId=packet['userId'], nextIndex=cursor, maxCount=100), session.user_agent_identity)
                current()
                _bounded(reply)
                if required(reply, 'userId', int) != packet['userId'] or required(reply, 'itemKind', int) != kind:
                    raise ValueError('item_binding')
                if 'returnCode' in reply and required(reply, 'returnCode', int) != 1:
                    raise ValueError('item_code')
                rows = required(reply, 'userItemList', list)
                validate_items(rows)
                for row in rows:
                    key = (row['itemKind'], row['itemId'])
                    if key in actual or row['itemKind'] != kind or len(actual) >= 100000:
                        raise ValueError('item_identity')
                    actual[key] = row
                cursor = required(reply, 'nextIndex', int)
                if cursor == 0:
                    break
                if not rows or not 0 < cursor <= 2**63-1 or cursor // 10_000_000_000 != kind:
                    raise ValueError('item_continuation')
            else:
                raise ValueError('item_budget')
        expected = _index(snapshot['nodes']['userItemList'], ('itemKind', 'itemId'))
        expected.update(_index(packet['upsertUserAll']['userItemList'], ('itemKind', 'itemId')))
        checks['items'] = actual == expected
    except Exception:
        pass
    return checks


def submit_typed(client, session, *, owner, private, snapshot, machine,
                 auth_date_time, catalog, operation, now=time.time):
    """Experimental single-attempt coordinator; no live acceptance is implied.

    Fixed results: rejected/reason, unchanged, ambiguous/checks,
    acknowledged_unverified/checks or verified/checks. Preexisting valid owned
    items do not claim or write; lower/equal scores reject score_not_improved.
    Readback checks exact target chart and every collected inventory/charge row.
    Only collected item kinds are attested. No raw server errors escape.
    """
    try:
        if type(owner) is not str or not owner.strip() or len(owner) > 256:
            raise ValueError('owner')
        binding = tuple(getattr(session, k) for k in ('user_id', 'login_id', 'login_date_time', 'user_agent_identity'))
        if any(type(v) is not int or v <= 0 for v in binding[:3]) or type(binding[3]) is not str or not binding[3]:
            raise ValueError('session')

        def current():
            client._current(session)
            if binding != tuple(getattr(session, k) for k in ('user_id', 'login_id', 'login_date_time', 'user_agent_identity')):
                raise ValueError('session_changed')

        current()
        _bounded(snapshot)
        _bounded(operation)
        snap, cabinet, op = deepcopy(snapshot), deepcopy(machine), deepcopy(operation)

        def build():
            return build_typed_settlement(snap, cabinet, user_id=binding[0], login_id=binding[1],
                login_date_time=binding[2], auth_date_time=auth_date_time, catalog=catalog,
                operation=op, now_unix=now())

        packet = build()
        current()
        if op['operation'] == 'item' and not packet['upsertUserAll']['userItemList']:
            return dict(status='unchanged')
    except _NotImproved:
        return dict(status='rejected', reason='score_not_improved')
    except Exception:
        return dict(status='rejected', reason='invalid_or_stale_input')
    try:
        if not _claim(private, binding, owner):
            return dict(status='rejected', reason='already_claimed')
    except Exception:
        return dict(status='rejected', reason='claim_failed')
    try:
        current()
        # Rebuilding rechecks schema, binding and freshness. Item gameplay wall
        # timestamps may advance; send the originally prepared packet once.
        fresh_packet = build()
        if op['operation'] == 'score' and fresh_packet != packet:
            raise ValueError('packet_changed')
    except Exception:
        return dict(status='rejected', reason='pre_send_validation_failed')
    try:
        reply = client._game('UpsertUserAllApi', deepcopy(packet), binding[3])
        acknowledged = type(reply) is dict and type(reply.get('returnCode')) is int and reply['returnCode'] == 1
        if acknowledged and 'userId' in reply:
            acknowledged = type(reply['userId']) is int and reply['userId'] == binding[0]
    except Exception:
        acknowledged = False
    checks = _readback(client, session, packet, snap, current)
    status = ('verified' if all(checks.values()) else 'acknowledged_unverified') if acknowledged else 'ambiguous'
    return dict(status=status, checks=checks)
