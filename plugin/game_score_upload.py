"""Single-attempt import of a native single-track post-play settlement.

Document v1 (plain JSON dict; no file or network loading here)::

    {"version": 1, "owner": "stable-account-owner",
     "session": {"userId": ..., "loginId": ..., "loginDateTime": ...,
                 "userAgentIdentity": ..., "utcOffsetSeconds": ...},
     "extend_flags": {"isPhotoAgree": ..., "isGotoCodeRead": ...},  # optional
     "baseline": <full pre-play upsertUserAll/UserAll object>,
     "request": <complete native UpsertUserAllApi request>}

The producer must capture this binding during the SAME active session; importing
or rewriting a previous session's packet is unsupported. This cannot authenticate
an untrusted producer. baseline is a full schema object, with empty game playlogs
and unsupported delta scopes. Its collected nodes must equal the separate fresh
settlement_collect snapshot. Unsupported scopes must be empty because collection
cannot attest them. Cosmetic/inventory/options/favorites changes are rejected.
Only one played chart/log is supported in v1. Judgments and profile/rating values
come from the native producer; this module never calculates or defaults them.

Caller holds the account/session lock from collection through return. private is
the same 0700 claim directory used by settlement_submit: claims survive failures,
crashes and ambiguous timeouts. Never remove a claim to retry. No runtime imports,
login, live networking at import, retry, publication, or raw response in results.
"""
from copy import deepcopy
from datetime import datetime, timezone
import math
import time

from basic_settlement import SCHEMA, check, required
from settlement import BITS_TO_ARRAY
from settlement_snapshot import DROP
from settlement_submit import _claim, _validate

BEST = ('achievement', 'deluxscoreMax', 'comboStatus', 'scoreRank')
NODES = {'userData', 'userExtend', 'userOption', 'userRatingList', 'userWeeklyData',
         'userChargeList', 'userFavoriteList', 'userActivityList', 'userMissionDataList',
         'userCharacterList', 'userMusicDetailList', 'userItemList'}
# Explicit native play effects only. Cosmetic selections, account identity,
# currency, unlocks, membership and unrelated settings remain immutable.
PROFILE = {'playCount', 'currentPlayCount', 'gradeRating', 'musicRating', 'playerRating',
           'highestRating', 'gradeRank', 'classRank', 'playerOldRating', 'playerNewRating',
           'dateTime', 'lastLoginDate', 'lastPlayDate', 'lastPlayCredit', 'lastPlayMode',
           'lastGameId', 'lastRomVersion', 'lastDataVersion', 'lastPlaceId', 'lastPlaceName',
           'lastAllNetId', 'lastRegionId', 'lastRegionName', 'lastClientId', 'lastCountryCode',
           'playVsCount', 'playSyncCount', 'winCount', 'helpCount', 'comboCount'}
PROFILE |= {k for k in SCHEMA['UserDetail'] if k.startswith(('totalDeluxscore', 'totalSync',
            'totalAchievement', 'totalBasic', 'totalAdvanced', 'totalExpert', 'totalMaster',
            'totalReMaster'))}
EXTEND = {'selectMusicId', 'selectDifficultyId', 'categoryIndex', 'musicIndex',
          'extraFlag', 'selectScoreType'}


def _bounded(value):
    """Bound before deepcopy/schema recursion, reject cycles and non-JSON values."""
    stack, containers, budget, size = [(value, 0)], set(), 400000, 0
    while stack:
        row, depth = stack.pop()
        budget -= 1
        if budget < 0 or depth > 32:
            raise ValueError('input_budget')
        if type(row) in (dict, list):
            if id(row) in containers:
                raise ValueError('aliased_or_cyclic_input')
            containers.add(id(row))
            if type(row) is dict:
                if any(type(k) is not str for k in row):
                    raise ValueError('invalid_key')
                size += sum(len(k) for k in row)
                stack.extend((v, depth + 1) for v in row.values())
            else:
                stack.extend((v, depth + 1) for v in row)
        elif type(row) is str:
            size += len(row)
        elif row is not None and type(row) not in (int, bool):
            raise ValueError('invalid_json')
        if size > 8_000_000:
            raise ValueError('input_size')


def _index(rows, keys):
    result = {}
    for row in rows:
        key = tuple(row[k] for k in keys)
        if key in result:
            raise ValueError('duplicate_identity')
        result[key] = row
    return result


def _bits(ua):
    for bits, array in BITS_TO_ARRAY.items():
        value = ua[bits]
        if bits == 'isNewFavoritemusicList':
            if value not in ('', '0', '1'):
                raise ValueError('invalid_bits')
        elif len(value) != len(ua[array]) or any(c not in ('12' if bits == 'isNewFavoriteList' else '01') for c in value):
            raise ValueError('invalid_bits')


def _validate_native(doc, snapshot, owner, binding, clock):
    keys = {'version', 'owner', 'session', 'baseline', 'request'}
    if set(doc) not in (keys, keys | {'extend_flags'}):
        raise ValueError('document_shape')
    if type(doc['version']) is not int or doc['version'] != 1 or doc['owner'] != owner:
        raise ValueError('document_binding')
    s = required(doc, 'session', dict)
    if set(s) != {'userId', 'loginId', 'loginDateTime', 'userAgentIdentity', 'utcOffsetSeconds'}:
        raise ValueError('session_shape')
    for key, typ, value in zip(('userId', 'loginId', 'loginDateTime', 'userAgentIdentity'),
                             (int, int, int, str), binding):
        if required(s, key, typ) != value:
            raise ValueError('session_binding')
    offset = required(s, 'utcOffsetSeconds', int)
    if not -50400 <= offset <= 50400:
        raise ValueError('offset')
    packet, base = required(doc, 'request', dict), required(doc, 'baseline', dict)
    _validate(packet, snapshot, {'utcOffsetSeconds': offset}, binding, clock)
    if snapshot.get('complete') is not True or required(snapshot, 'capturedAt', int) < binding[2] - offset:
        raise ValueError('incomplete_snapshot')
    nodes = required(snapshot, 'nodes', dict)
    if set(nodes) != NODES:
        raise ValueError('snapshot_shape')
    check(base, 'UserAll', 'baseline')
    _bits(base)
    ua = packet['upsertUserAll']
    _bits(ua)
    for name in ('userData', 'userOption', 'userExtend', 'userRatingList'):
        if len(base[name]) != 1 or len(ua[name]) != 1:
            raise ValueError('singleton')
    for name in NODES:
        if nodes[name] != base[name]:
            raise ValueError('baseline_snapshot_mismatch')
    for name, keys in (('userChargeList', ('chargeId',)), ('userMusicDetailList', ('musicId', 'level')),
                       ('userItemList', ('itemKind', 'itemId')), ('userCharacterList', ('characterId',)),
                       ('userFavoriteList', ('itemKind',))):
        _index(base[name], keys)
    mutable = {'userData', 'userExtend', 'userRatingList', 'userMusicDetailList',
               'isNewMusicDetailList', 'userGamePlaylogList', 'userCharacterList',
               'isNewCharacterList', 'userItemList', 'isNewItemList'}
    for name in SCHEMA['UserAll']:
        if name not in mutable and ua[name] != base[name]:
            raise ValueError('unrelated_mutation')
        if name not in NODES and name not in BITS_TO_ARRAY and name != 'user2pPlaylog' and base[name] != []:
            raise ValueError('unsupported_baseline_scope')
    # These wire scopes are deltas: empty means no change, or the producer may
    # echo unchanged collected rows with existing-record flags.
    for name, bits in (('userCharacterList', 'isNewCharacterList'),
                       ('userItemList', 'isNewItemList')):
        if ua[name] not in ([], base[name]) or ua[bits] != '0' * len(ua[name]):
            raise ValueError('inventory_or_character_mutation')
    pair = base['user2pPlaylog']
    if pair['user2pPlaylogDetailList'] or pair['userId1'] or pair['userId2']:
        raise ValueError('unsupported_pair_play')
    if ua['userGamePlaylogList'][0]['useTicketId'] != -1:
        raise ValueError('ticket_consumption')
    for name, allowed in (('userData', PROFILE), ('userExtend', EXTEND)):
        for key, value in base[name][0].items():
            if key not in allowed and ua[name][0][key] != value:
                raise ValueError('unrelated_profile_mutation')
    # Chart identity is the protocol musicId (including DX namespace) and level.
    log = packet['userPlaylogList'][0]
    if log['userId'] not in (0, binding[0]) or log['trackNo'] != 1 or log['musicId'] <= 0 or not 0 <= log['level'] <= 4:
        raise ValueError('invalid_played_chart')
    if log['isEventMode'] != packet['isEventMode']:
        raise ValueError('event_binding')
    # Native strings express cabinet wall time; snapshot/clock use real UTC.
    def instant(value):
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=timezone.utc).timestamp() - offset
    play = instant(log['userPlayDate'])
    settled = instant(ua['userGamePlaylogList'][0]['playDate'])
    if not binding[2] - offset <= play <= settled <= clock:
        raise ValueError('native_time_order')
    if any(log[name + suffix] < 0 for name in ('tap', 'hold', 'slide', 'touch', 'break')
           for suffix in ('CriticalPerfect', 'Perfect', 'Great', 'Good', 'Miss')):
        raise ValueError('negative_judgments')
    if len(ua['userMusicDetailList']) != 1:
        raise ValueError('single_track_required')
    detail = ua['userMusicDetailList'][0]
    identity = (log['musicId'], log['level'])
    if (detail['musicId'], detail['level']) != identity:
        raise ValueError('chart_mismatch')
    old = _index(base['userMusicDetailList'], ('musicId', 'level')).get(identity)
    if detail['playCount'] != (old['playCount'] if old else 0) + 1:
        raise ValueError('chart_playcount')
    if ua['isNewMusicDetailList'] != ('0' if old else '1'):
        raise ValueError('new_chart_flag')
    if detail['extNum1'] < (old['extNum1'] if old else 0) or (old and old['playCount'] < 0):
        raise ValueError('chart_counter_decreased')
    # extNum1 is a native threshold counter with special 1/2 increments. Preserve
    # the producer value instead of treating it as immutable or deriving it.
    if any(not 0 <= value <= 13 for value in (log['scoreRank'], detail['scoreRank'])):
        raise ValueError('invalid_score_rank')
    for key in BEST:
        if detail[key] < (old[key] if old else 0):
            raise ValueError('best_decreased')
    for key, logkey in zip(BEST, ('achievement', 'deluxscore', 'comboStatus', 'scoreRank')):
        if log[logkey] < 0 or detail[key] < log[logkey]:
            raise ValueError('best_below_play')
    def merge_sync(previous, played):
        if played == 5:
            return 5 if previous == 0 else previous
        if previous == 5:
            return played if played else previous
        return max(previous, played)
    previous = old['syncStatus'] if old else 0
    if any(type(v) is not int or not 0 <= v <= 5
           for v in (previous, log['syncStatus'], detail['syncStatus'])):
        raise ValueError('sync_status')
    if detail['syncStatus'] != merge_sync(previous, log['syncStatus']):
        raise ValueError('sync_merge')
    for key in ('playCount', 'currentPlayCount'):
        if base['userData'][0][key] < 0 or ua['userData'][0][key] != base['userData'][0][key] + 1:
            raise ValueError('profile_playcount')
    if ua['userGamePlaylogList'][0]['playTrack'] != 1:
        raise ValueError('track_count')


def _readback(client, session, packet, current):
    checks = dict(charts=False, charges=False)

    def read(api, fields=None):
        current()
        reply = client._game(api, dict(userId=session.user_id, **(fields or {})), session.user_agent_identity)
        current()
        _bounded(reply)
        if required(reply, 'userId', int) != packet['userId']:
            raise ValueError('readback_binding')
        if 'returnCode' in reply and required(reply, 'returnCode', int) != 1:
            raise ValueError('readback_code')
        return reply

    try:
        cursor, seen, charts = 0, set(), {}
        for _ in range(128):
            if cursor in seen:
                raise ValueError('music_cycle')
            seen.add(cursor)
            reply = read('GetUserMusicApi', dict(nextIndex=cursor, maxCount=50))
            count = 0
            for group in required(reply, 'userMusicList', list):
                for raw in required(group, 'userMusicDetailList', list):
                    row = deepcopy(raw)
                    for key in DROP['userMusicDetailList']:
                        row.pop(key, None)
                    check(row, 'UserMusicDetail', 'chart')
                    key = (row['musicId'], row['level'])
                    if key in charts or len(charts) >= 100000:
                        raise ValueError('music_budget_or_duplicate')
                    charts[key] = row
                    count += 1
            cursor = required(reply, 'nextIndex', int)
            if cursor == 0:
                checks['charts'] = all(charts.get((r['musicId'], r['level'])) == r
                                       for r in packet['upsertUserAll']['userMusicDetailList'])
                break
            if not 0 < cursor <= 2**63-1 or not count:
                raise ValueError('music_continuation')
    except Exception:
        pass
    try:
        reply = read('GetUserChargeApi')
        if 'nextIndex' in reply and required(reply, 'nextIndex', int) != 0:
            raise ValueError('charge_continuation')
        rows = deepcopy(required(reply, 'userChargeList', list))
        if 'length' in reply and required(reply, 'length', int) != len(rows):
            raise ValueError('charge_length')
        for row in rows:
            for key in DROP['userChargeList']:
                row.pop(key, None)
        check(rows, SCHEMA['UserAll']['userChargeList'], 'charges')
        checks['charges'] = _index(rows, ('chargeId',)) == _index(packet['upsertUserAll']['userChargeList'], ('chargeId',))
    except Exception:
        pass
    return checks


def submit_native(client, session, *, owner, private, document, snapshot, now=time.time):
    """Validate, durably claim, write once, GET all music pages and charges.

    Returns fixed status/reason and boolean checks only. verified attests the exact
    uploaded chart fields and preserved charges, not every profile/rating effect.
    A transport error or non-success acknowledgement stays ambiguous even when
    readback matches. All post-claim failures consume the session's shared claim.
    """
    try:
        if type(owner) is not str or not owner.strip() or len(owner) > 256:
            raise ValueError('owner')
        binding = tuple(getattr(session, k) for k in ('user_id', 'login_id', 'login_date_time', 'user_agent_identity'))
        if any(type(v) is not int or v <= 0 for v in binding[:3]) or type(binding[3]) is not str:
            raise ValueError('session')

        def current():
            client._current(session)
            if binding != tuple(getattr(session, k) for k in ('user_id', 'login_id', 'login_date_time', 'user_agent_identity')):
                raise ValueError('session_changed')

        def validate():
            clock = now()
            if type(clock) not in (int, float) or not math.isfinite(clock):
                raise ValueError('clock')
            _validate_native(doc, snap, owner, binding, clock)

        current()
        _bounded(document)
        _bounded(snapshot)
        doc, snap = deepcopy(document), deepcopy(snapshot)
        validate()
        current()
    except Exception:
        return dict(status='rejected', reason='invalid_or_stale_input')
    try:
        if not _claim(private, binding, owner):
            return dict(status='rejected', reason='already_claimed')
    except Exception:
        return dict(status='rejected', reason='claim_failed')
    try:
        current()
        validate()
    except Exception:
        return dict(status='rejected', reason='pre_send_validation_failed')
    packet = doc['request']
    try:
        reply = client._game('UpsertUserAllApi', deepcopy(packet), binding[3])
        acknowledged = type(reply) is dict and type(reply.get('returnCode')) is int and reply['returnCode'] == 1
        if acknowledged and 'userId' in reply:
            acknowledged = type(reply['userId']) is int and reply['userId'] == binding[0]
    except Exception:
        acknowledged = False
    checks = _readback(client, session, packet, current)
    status = ('verified' if all(checks.values()) else 'acknowledged_unverified') if acknowledged else 'ambiguous'
    return dict(status=status, checks=checks)


def submit_scores(client, session, *, owner, private, snapshot, document, now=time.time):
    """CLI adapter for submit_native; requires a genuine current-session packet.

    Optional document.extend_flags belongs to collection orchestration; native
    userExtend and its fresh baseline are still checked without modification.
    """
    return submit_native(client, session, owner=owner, private=private,
                         document=document, snapshot=snapshot, now=now)
