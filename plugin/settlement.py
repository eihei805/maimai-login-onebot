"""Explicit game settlement + cosmetic delta; no synthesized gameplay or GET mapping.
Schema names: Project-Upsert api/upsert_user_all.py; item/session semantics: game IL.
A producer must export a complete upload-format baseline and post-play request.
"""
from copy import deepcopy
from item_upload import add_collections, merge_item_delta, validate_items

NODE_TYPES = {'userData': 'singleton', 'userExtend': 'singleton', 'userOption': 'singleton', 'userRatingList': 'singleton', 'userWeeklyData': 'object', 'user2pPlaylog': 'object', 'userCharacterList': 'array', 'userGhost': 'array', 'userMapList': 'array', 'userLoginBonusList': 'array', 'userItemList': 'array', 'userMusicDetailList': 'array', 'userCourseList': 'array', 'userFriendSeasonRankingList': 'array', 'userChargeList': 'array', 'userFavoriteList': 'array', 'userActivityList': 'array', 'userMissionDataList': 'array', 'userGamePlaylogList': 'array', 'userIntimateList': 'array', 'userShopItemStockList': 'array', 'userGetPointList': 'array', 'userTradeItemList': 'array', 'userFavoritemusicList': 'array', 'userKaleidxScopeList': 'array', 'isNewCharacterList': 'bits', 'isNewMapList': 'bits', 'isNewLoginBonusList': 'bits', 'isNewItemList': 'bits', 'isNewMusicDetailList': 'bits', 'isNewCourseList': 'bits', 'isNewFavoriteList': 'bits', 'isNewFriendSeasonRankingList': 'bits', 'isNewUserIntimateList': 'bits', 'isNewFavoritemusicList': 'bits', 'isNewKaleidxScopeList': 'bits'}
BITS_TO_ARRAY = {'isNewCharacterList': 'userCharacterList', 'isNewMapList': 'userMapList', 'isNewLoginBonusList': 'userLoginBonusList', 'isNewItemList': 'userItemList', 'isNewMusicDetailList': 'userMusicDetailList', 'isNewCourseList': 'userCourseList', 'isNewFavoriteList': 'userFavoriteList', 'isNewFriendSeasonRankingList': 'userFriendSeasonRankingList', 'isNewUserIntimateList': 'userIntimateList', 'isNewFavoritemusicList': 'userFavoritemusicList', 'isNewKaleidxScopeList': 'userKaleidxScopeList'}
PLAYLOG_FIELDS = ('userId', 'orderId', 'playlogId', 'version', 'placeId', 'placeName', 'loginDate', 'playDate', 'userPlayDate', 'type', 'musicId', 'level', 'trackNo', 'vsMode', 'vsUserName', 'vsStatus', 'vsUserRating', 'vsUserAchievement', 'vsUserGradeRank', 'vsRank', 'playerNum', 'playedUserId1', 'playedUserName1', 'playedMusicLevel1', 'playedUserId2', 'playedUserName2', 'playedMusicLevel2', 'playedUserId3', 'playedUserName3', 'playedMusicLevel3', 'characterId1', 'characterLevel1', 'characterAwakening1', 'characterId2', 'characterLevel2', 'characterAwakening2', 'characterId3', 'characterLevel3', 'characterAwakening3', 'characterId4', 'characterLevel4', 'characterAwakening4', 'characterId5', 'characterLevel5', 'characterAwakening5', 'achievement', 'deluxscore', 'scoreRank', 'maxCombo', 'totalCombo', 'maxSync', 'totalSync', 'tapCriticalPerfect', 'tapPerfect', 'tapGreat', 'tapGood', 'tapMiss', 'holdCriticalPerfect', 'holdPerfect', 'holdGreat', 'holdGood', 'holdMiss', 'slideCriticalPerfect', 'slidePerfect', 'slideGreat', 'slideGood', 'slideMiss', 'touchCriticalPerfect', 'touchPerfect', 'touchGreat', 'touchGood', 'touchMiss', 'breakCriticalPerfect', 'breakPerfect', 'breakGreat', 'breakGood', 'breakMiss', 'isTap', 'isHold', 'isSlide', 'isTouch', 'isBreak', 'isCriticalDisp', 'isFastLateDisp', 'fastCount', 'lateCount', 'isAchieveNewRecord', 'isDeluxscoreNewRecord', 'comboStatus', 'syncStatus', 'isClear', 'beforeRating', 'afterRating', 'beforeGrade', 'afterGrade', 'afterGradeRank', 'beforeDeluxRating', 'afterDeluxRating', 'isPlayTutorial', 'isEventMode', 'isFreedomMode', 'playMode', 'isNewFree', 'trialPlayAchievement', 'extNum1', 'extNum2', 'extNum4', 'extBool1', 'extBool2', 'extBool3')


def checked_user_all(value):
    if type(value) is not dict or not NODE_TYPES.keys() <= value.keys():
        raise ValueError('complete_upload_format_baseline_required')
    for name, kind in NODE_TYPES.items():
        node = value[name]
        if name == 'userGhost' and node is None:
            continue  # ExportUserAll emits null unless NeedSave.
        if kind == 'singleton':
            ok = type(node) is list and len(node) == 1 and type(node[0]) is dict and bool(node[0])
        elif kind == 'object':
            ok = type(node) is dict
        elif kind == 'bits':
            ok = type(node) is str
        else:
            ok = type(node) is list and all(type(row) is dict for row in node)
        if not ok:
            raise ValueError('invalid_settlement_node_' + name)
    for bits, array in BITS_TO_ARRAY.items():
        # Favorite music is a replacement flag, not a per-row bit string.
        if bits == 'isNewFavoritemusicList':
            continue
        allowed = '12' if bits == 'isNewFavoriteList' else '01'
        if len(value[bits]) != len(value[array]) or any(c not in allowed for c in value[bits]):
            raise ValueError('invalid_settlement_bits')


def prepare(document, owner, session, kind, ids):
    """Use explicitly supplied complete baseline and post-play request unchanged.

    Completeness of nested version-specific profiles is the exporting producer's
    responsibility; preserve every baseline field, reject dropped snapshot keys.
    No opaque JSON supplied in a chat message is accepted by the command layer.
    """
    if type(document) is not dict or document.get('version') != 1 or document.get('owner') != owner:
        raise ValueError('settlement_owner_rejected')
    baseline, request = document.get('baseline'), document.get('request')
    checked_user_all(baseline)
    if type(request) is not dict:
        raise ValueError('settlement_request_required')
    if any(type(request.get(k)) is not int or request[k] != v for k, v in
           (('userId', session.user_id), ('playlogId', session.login_id), ('loginDateTime', session.login_date_time))):
        raise ValueError('settlement_session_mismatch')
    ua = request.get('upsertUserAll')
    checked_user_all(ua)
    for node in ('userData', 'userExtend', 'userOption', 'userRatingList'):
        if not baseline[node][0].keys() <= ua[node][0].keys():
            raise ValueError('baseline_snapshot_fields_dropped')
    logs = request.get('userPlaylogList')
    if type(logs) is not list or not logs or not ua['userMusicDetailList'] or not ua['userGamePlaylogList']:
        raise ValueError('explicit_gameplay_required')
    for row in logs:
        if type(row) is not dict or not set(PLAYLOG_FIELDS) <= row.keys():
            raise ValueError('complete_playlog_required')
        if type(row['playlogId']) is not int or row['playlogId'] != session.login_id:
            raise ValueError('playlog_session_mismatch')
        if not any(detail.get('musicId') == row['musicId'] and detail.get('level') == row['level']
                   for detail in ua['userMusicDetailList']):
            raise ValueError('played_music_detail_required')
    inventory = document.get('inventory')
    validate_items(inventory)
    current = add_collections(inventory, kind, ids)
    return merge_item_delta(request, inventory, current)


def inventory(client, session, kind, budget=512):
    """Read complete affected kind; never treat an empty nonterminal page as EOF."""
    cursor, seen, rows = kind * 10000000000, set(), []
    for _ in range(budget):
        if cursor in seen:
            raise ValueError('item_cursor_cycle')
        seen.add(cursor)
        reply = client._game('GetUserItemApi', {'userId': session.user_id,
                             'nextIndex': cursor, 'maxCount': 100}, session.user_agent_identity)
        if ('returnCode' in reply and (type(reply['returnCode']) is not int or reply['returnCode'] != 1)
            or 'userId' in reply and reply['userId'] != session.user_id):
            raise ValueError('item_response_rejected')
        page, cursor = reply.get('userItemList'), reply.get('nextIndex')
        validate_items(page)
        if any(r['itemKind'] != kind for r in page) or type(cursor) is not int or cursor < 0:
            raise ValueError('item_page_rejected')
        rows.extend(page)
        validate_items(rows)
        if cursor == 0:
            return rows
    raise ValueError('item_page_budget')


def execute(document, owner, client, session, kind, ids, claim):
    """Caller holds the shared session lock. claim durably tombstones BEFORE send.

    No retries, login, QR use, logout, or session deadline changes. Even ambiguous
    writes consume this session's settlement allowance. Readback confirms items,
    not every aspect of the game's settlement processing.
    """
    client._current(session)
    request = prepare(document, owner, session, kind, ids)
    delta = request['upsertUserAll']['userItemList']
    if not delta:
        return {'status': 'unchanged', 'changed': 0}
    fresh = inventory(client, session, kind)
    expected = [r for r in document['inventory'] if r['itemKind'] == kind]
    key = lambda r: (r['itemKind'], r['itemId'])
    if sorted(fresh, key=key) != sorted(expected, key=key):
        raise ValueError('inventory_baseline_changed')
    claim()
    try:
        reply = client._game('UpsertUserAllApi', deepcopy(request), session.user_agent_identity)
        if type(reply.get('returnCode')) is not int or reply['returnCode'] != 1:
            return {'status': 'unconfirmed', 'changed': len(delta)}
        actual = {key(r): r for r in inventory(client, session, kind)}
        if all(actual.get(key(r)) == r for r in delta):
            return {'status': 'items_verified', 'changed': len(delta)}
    except Exception:
        pass
    return {'status': 'unconfirmed', 'changed': len(delta)}
