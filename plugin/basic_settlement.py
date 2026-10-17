"""Offline, explicit-input single-track settlement. No network or runtime config imports.

Wire schema is extracted from the supplied game's Net.VO.Mai2 metadata, not
Project-Upsert's permissive defaults. Inputs are normalized fresh save snapshots.
Freshness metadata is a caller assertion: the submitting coordinator must hold the
account lock from collection through submission and never retry an ambiguous write.
"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from item_upload import add_collections, item_delta, validate_items

SCHEMA = json.loads(Path(__file__).with_name('settlement_schema.json').read_text())['types']


def check(value, kind, path):
    if isinstance(kind, dict):
        if type(value) is not list:
            raise ValueError(path + ': expected array')
        for i, row in enumerate(value):
            check(row, kind['array'], f'{path}[{i}]')
    elif kind in SCHEMA:
        if type(value) is not dict:
            raise ValueError(path + ': expected object')
        missing, extra = set(SCHEMA[kind]) - set(value), set(value) - set(SCHEMA[kind])
        if missing or extra:
            raise ValueError(f'{path}: missing={sorted(missing)}, unknown={sorted(extra)}')
        for key, typ in SCHEMA[kind].items():
            # Preserve explicitly observed server nulls in identity/history fields.
            # No replacement history is invented; missing fields still fail above.
            if (kind == 'UserDetail' and key in
                    {'accessCode', 'lastGameId', 'lastPlaceName', 'lastClientId', 'dateTime'}
                    and value[key] is None):
                continue
            check(value[key], typ, path + '.' + key)
    elif kind in ('byte', 'int', 'uint', 'long', 'ulong'):
        bits = 8 if kind == 'byte' else 64 if kind in ('long', 'ulong') else 32
        unsigned = kind in ('byte', 'uint', 'ulong')
        low, high = (0, 2**bits - 1) if unsigned else (-2**(bits-1), 2**(bits-1)-1)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(path + ': invalid ' + kind)
    elif kind in ('str', 'bool'):
        if type(value) is not {'str': str, 'bool': bool}[kind]:
            raise ValueError(path + ': invalid ' + kind)
    else:
        raise ValueError(path + ': unresolved protocol type ' + kind)


def required(obj, key, typ):
    if type(obj) is not dict or key not in obj or type(obj[key]) is not typ:
        raise ValueError(key + ': missing or invalid explicit input')
    return obj[key]


def wall(moment):
    return datetime.fromtimestamp(moment, timezone.utc).strftime('%Y-%m-%d %H:%M:%S.0')


def build_basic_settlement(snapshot, machine, *, user_id, login_id, login_date_time,
                           auth_date_time, play_date_time, settle_date_time,
                           now_unix, catalog, kind=1, item_ids=None):
    """Build one authorized all-miss SD BASIC ID8 play plus cosmetic delta.

    snapshot: userId/loginId/loginDateTime, capturedAt (real Unix seconds),
      complete=True, nodes (full normalized baseline), itemKinds (complete scopes).
    machine: all keys used below must be explicit, including playSpecial (signed
      encoded machine bitmask), localPlayCount, time offset and UI state.
    Grade/rank and judgement display are derived from the player snapshot.
    No defaults for account, machine, times, weekly data or unknown wire fields.
    """
    for name, value in dict(userId=user_id, loginId=login_id,
                            loginDateTime=login_date_time, authDateTime=auth_date_time,
                            playDateTime=play_date_time, settleDateTime=settle_date_time,
                            nowUnix=now_unix).items():
        if type(value) is not int or value <= 0:
            raise ValueError(name + ': positive integer required')
    for key, value in [('userId', user_id), ('loginId', login_id), ('loginDateTime', login_date_time)]:
        if required(snapshot, key, int) != value:
            raise ValueError('snapshot.' + key + ': session mismatch')
    if required(snapshot, 'complete', bool) is not True:
        raise ValueError('snapshot.complete: incomplete collection')
    offset = required(machine, 'utcOffsetSeconds', int)
    if not -50400 <= offset <= 50400:
        raise ValueError('utcOffsetSeconds: invalid offset')
    captured = required(snapshot, 'capturedAt', int)
    if not login_date_time-offset <= captured <= play_date_time-offset <= settle_date_time-offset <= now_unix:
        raise ValueError('snapshot/timestamps: invalid session ordering')
    if now_unix - captured > 300:
        raise ValueError('snapshot.capturedAt: older than 300 seconds; recollect')
    if type(kind) is not int or kind not in required(snapshot, 'itemKinds', list):
        raise ValueError('snapshot.itemKinds: requested inventory scope is incomplete')
    rows = [r for r in catalog if str(r.get('id')) == '8']
    if len(rows) != 1 or rows[0].get('title') != 'True Love Song' or rows[0].get('type') != 'SD' or rows[0]['charts'][0]['notes'] != [63, 23, 8, 2]:
        raise ValueError('catalog: expected True Love Song SD BASIC notes 63/23/8/2')
    base = deepcopy(required(snapshot, 'nodes', dict))
    expected_nodes = {'userData', 'userExtend', 'userOption', 'userRatingList',
                      'userWeeklyData', 'userChargeList', 'userFavoriteList',
                      'userActivityList', 'userMissionDataList', 'userCharacterList',
                      'userMusicDetailList', 'userItemList'}
    if set(base) != expected_nodes:
        raise ValueError('snapshot.nodes: missing=' + str(sorted(expected_nodes-set(base))) +
                         ', unknown=' + str(sorted(set(base)-expected_nodes)))
    singleton = {'userData': 'UserDetail', 'userExtend': 'UserExtend',
                 'userOption': 'UserOption', 'userRatingList': 'UserRating'}
    for node, typ in singleton.items():
        values = required(base, node, list)
        if len(values) != 1:
            raise ValueError(node + ': expected one full snapshot')
        check(values[0], typ, node + '[0]')
    for node in ('userWeeklyData', 'userChargeList', 'userFavoriteList',
                 'userActivityList', 'userMissionDataList', 'userCharacterList',
                 'userMusicDetailList', 'userItemList'):
        if node not in base:
            raise ValueError(node + ': missing fresh snapshot')
        check(base[node], SCHEMA['UserAll'][node], node)
    validate_items(base['userItemList'])
    ud, ue = base['userData'][0], base['userExtend'][0]
    if ud['playCount'] < 0 or ud['currentPlayCount'] < 0:
        raise ValueError('userData: negative play counter')
    chars = base['userCharacterList']
    by_id = {r['characterId']: r for r in chars}
    if len(by_id) != len(chars) or len(ud['charaSlot']) != 5:
        raise ValueError('userCharacterList/charaSlot: duplicate identity or invalid slots')
    if any(cid not in by_id for cid in ud['charaSlot']):
        raise ValueError('userCharacterList: missing selected character snapshot')
    music = base['userMusicDetailList']
    keys = [(r['musicId'], r['level']) for r in music]
    if len(set(keys)) != len(keys):
        raise ValueError('userMusicDetailList: duplicate chart')
    old = next((r for r in music if (r['musicId'], r['level']) == (8, 0)), None)
    detail = deepcopy(old) if old is not None else dict(musicId=8, level=0, playCount=0,
        achievement=0, comboStatus=0, syncStatus=0, deluxscoreMax=0, scoreRank=0, extNum1=0)
    detail['playCount'] += 1  # all-miss never replaces any prior best field
    # All 110 fields have explicit solo/all-miss semantics; unknown types fail.
    log = {}
    for key, typ in SCHEMA['UserPlaylog'].items():
        if typ not in ('int', 'uint', 'long', 'ulong', 'bool', 'str'):
            raise ValueError('UserPlaylog.' + key + ': unresolved default semantics')
        log[key] = '' if typ == 'str' else False if typ == 'bool' else 0
    for key in ('version', 'placeId', 'playMode'):
        log[key] = required(machine, key, int)
    for key in ('placeName',):
        log[key] = required(machine, key, str)
    for key in ('isEventMode', 'isNewFree'):
        log[key] = required(machine, key, bool)
    # ExportUserPlaylog reads these from player state, not cabinet config.
    judge = base['userOption'][0]['dispJudge']
    class_value = base['userRatingList'][0]['udemae']['classValue']
    rank = abs(class_value) // 10000
    if class_value < 0:
        rank = -rank  # C# integer division truncates toward zero.
    log.update(beforeGrade=ud['gradeRating'], afterGradeRank=min(rank, 25),
               isCriticalDisp=5 <= judge <= 14, isFastLateDisp=True)
    # Full rate recalculation is not yet implemented. Fail closed rather than
    # emit known-incorrect native afterGrade/rating values for legacy saves.
    if ud['gradeRating'] != 0 or ud['playerRating'] != ud['musicRating']:
        raise ValueError('userData.rating: native rate recalculation required')
    log['afterGrade'] = 0
    dt = datetime.fromtimestamp(play_date_time, timezone.utc)
    day = dt - timedelta(days=1) if dt.hour < 7 else dt
    log.update(userId=0, orderId=0, playlogId=login_id, loginDate=login_date_time-offset,
               playDate=day.strftime('%Y-%m-%d'), userPlayDate=wall(play_date_time),
               musicId=8, level=0, trackNo=1, playerNum=1, totalCombo=96,
               beforeRating=ud['musicRating'], afterRating=ud['musicRating'],
               beforeDeluxRating=ud['musicRating'], afterDeluxRating=ud['musicRating'],
               trialPlayAchievement=-1, extNum4=required(machine, 'categoryIndex', int))
    for name, count in zip(('tap', 'hold', 'slide', 'touch', 'break'), (63, 23, 8, 0, 2)):
        log[name+'Miss'] = count
        log['is'+name.capitalize()] = count > 0
    for index, cid in enumerate(ud['charaSlot'], 1):
        c = by_id[cid]
        for target, source in [('Id', 'characterId'), ('Level', 'level'), ('Awakening', 'awakening')]:
            log[f'character{target}{index}'] = c[source]
    ua = {key: '' if typ == 'str' else [] for key, typ in SCHEMA['UserAll'].items()}
    for node in singleton:
        ua[node] = base[node]
    for node in ('userWeeklyData', 'userChargeList', 'userFavoriteList', 'userActivityList', 'userMissionDataList'):
        ua[node] = base[node]
    # Favorite snapshots must retain raw GET kind (including null) separately.
    raw = required(snapshot, 'rawFavoriteKinds', list)
    fav = base['userFavoriteList']
    if len(raw) != len(fav) or len({r['itemKind'] for r in fav}) != len(fav):
        raise ValueError('rawFavoriteKinds: missing/duplicate favorite scope')
    for value, row in zip(raw, fav):
        if value is not None and (type(value) is not int or value != row['itemKind']):
            raise ValueError('rawFavoriteKinds: inconsistent category')
    if {r['itemKind'] for r in fav} != {1, 2, 3, 10, 11}:
        raise ValueError('userFavoriteList: require all five cosmetic categories')
    ua['isNewFavoriteList'] = ''.join('1' if k is None else '2' for k in raw)
    ua['user2pPlaylog'] = dict(userId1=0, userId2=0, userName1='', userName2='',
                              regionId=0, placeId=0, user2pPlaylogDetailList=[])
    ua['userMusicDetailList'] = [detail]
    ua['isNewMusicDetailList'] = '1' if old is None else '0'
    # Explicit [] is a no-cosmetic play (ticket workflows); None retains the
    # historical BASIC fixture default. Other malformed values still fail.
    current_items = (base['userItemList'] if type(item_ids) is list and not item_ids
                     else add_collections(base['userItemList'], kind, [11] if item_ids is None else item_ids))
    ua.update(item_delta(base['userItemList'], current_items))
    ud['playCount'] += 1
    ud['currentPlayCount'] += 1
    ud.update(lastPlayDate=wall(settle_date_time), lastLoginDate=wall(login_date_time),
              lastPlayMode=log['playMode'], lastPlayCredit=required(machine, 'playCredit', int),
              dateTime=auth_date_time)
    for target, source, typ in [('lastPlaceId','placeId',int), ('lastPlaceName','placeName',str),
            ('lastRegionId','regionId',int), ('lastRegionName','regionName',str),
            ('lastClientId','clientId',str), ('lastGameId','gameId',str),
            ('lastCountryCode','countryCode',str), ('lastRomVersion','versionString',str),
            ('lastDataVersion','dataVersion',str)]:
        ud[target] = required(machine, source, typ)
    ue.update(selectMusicId=8, selectDifficultyId=0)
    for key in ('categoryIndex', 'musicIndex', 'extraFlag', 'selectScoreType'):
        ue[key] = required(machine, key, int)
    if ue['selectScoreType'] != 0:
        raise ValueError('selectScoreType: SD chart requires 0')
    ua['userGamePlaylogList'] = [dict(playlogId=login_id, version=machine['versionString'],
        playDate=wall(settle_date_time), playMode=log['playMode'], useTicketId=-1,
        playCredit=machine['playCredit'], playTrack=1, clientId=machine['clientId'],
        isPlayTutorial=False, isEventMode=log['isEventMode'], isNewFree=log['isNewFree'],
        playCount=required(machine, 'localPlayCount', int),
        playSpecial=required(machine, 'playSpecial', int), playOtherUserId=0)]
    check(log, 'UserPlaylog', 'userPlaylogList[0]')
    check(ua, 'UserAll', 'upsertUserAll')
    return dict(userId=user_id, playlogId=login_id, loginDateTime=login_date_time,
                isEventMode=log['isEventMode'], isFreePlay=required(machine, 'isFreePlay', bool),
                userPlaylogList=[log], upsertUserAll=ua)
