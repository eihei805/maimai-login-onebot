"""Strict pure GET-to-save normalization; no network and no missing-field defaults.

Callers collect complete responses under their account lock. Raw response keys are
node names below. Character/music/item lists must already be fully paginated.
Unsupported response variants deliberately fail instead of losing save fields.
"""
from copy import deepcopy
from basic_settlement import SCHEMA, check, required

DROP = {
 'userData': {'friendCode', 'nameplateId', 'trophyId', 'cmLastEmoneyCredit', 'cmLastEmoneyBrand'},
 'userOption': {'tempoVolume'}, 'userMusicDetailList': {'extNum2'},
 'userChargeList': {'extNum1'}, 'userMissionDataList': {'userId'},
 'userWeeklyData': {'userId'},
 'userCharacterList': {'nextAwake', 'nextAwakePercent', 'point'},
}


def normalize_snapshot(raw, *, user_id, login_id, login_date_time, captured_at,
                       complete, item_kinds, extend_flags):
    """Normalize explicit collected data to build_basic_settlement's snapshot.

    raw contains node values (not arbitrary response wrappers); userData is the
    whole GetUserDataApi reply to retain its top-level banState. userFavoriteList
    contains five {requestedKind, response} records; response is raw userFavorite.
    Only documented GET-only fields are removed. Unknown/missing fields fail.
    """
    out = {}
    names = ('userData','userExtend','userOption','userRatingList','userWeeklyData',
             'userChargeList','userFavoriteList','userActivityList','userMissionDataList',
             'userCharacterList','userMusicDetailList','userItemList')
    for name in names:
        if name not in raw: raise ValueError(name + ': missing collected snapshot')
        out[name] = deepcopy(raw[name])
    reply = out['userData']
    if required(reply, 'userId', int) != user_id:
        raise ValueError('userData.userId: snapshot account mismatch')
    ud = deepcopy(required(reply, 'userData', dict))
    ban = required(reply, 'banState', int)
    if 'banState' in ud and ud['banState'] != ban:
        raise ValueError('userData.banState: conflicting response fields')
    ud['banState'] = ban
    out['userData'] = ud
    for name, drops in DROP.items():
        value = out[name]
        rows = value if type(value) is list else [value]
        for row in rows:
            if type(row) is not dict: raise ValueError(name + ': expected object')
            for key in drops: row.pop(key, None)
    for key in ('isPhotoAgree', 'isGotoCodeRead'):
        flag = required(extend_flags, key, bool)
        if key in out['userExtend'] and out['userExtend'][key] != flag:
            raise ValueError('userExtend.' + key + ': conflicting explicit state')
        out['userExtend'][key] = flag
    udemae = required(out['userRatingList'], 'udemae', dict)
    for key in ('MaxLoseNum','NpcTotalWinNum','NpcTotalLoseNum','NpcMaxWinNum',
                'NpcMaxLoseNum','NpcWinNum','NpcLoseNum'):
        udemae.pop(key, None)
    raw_kinds, favorites = [], []
    for entry in out['userFavoriteList']:
        kind = required(entry, 'requestedKind', int)
        response = required(entry, 'response', dict)
        if set(response) != {'itemKind','itemIdList'}:
            raise ValueError('userFavoriteList: unrecognized raw favorite response')
        actual = response['itemKind']
        if actual is not None and (type(actual) is not int or actual != kind):
            raise ValueError('userFavoriteList: response kind mismatch')
        if actual is None and response['itemIdList'] != []:
            raise ValueError('userFavoriteList: null category with nonempty items')
        raw_kinds.append(actual)
        favorites.append(dict(itemKind=kind,itemIdList=deepcopy(response['itemIdList'])))
    out['userFavoriteList'] = favorites
    for name in ('userData','userExtend','userOption','userRatingList'):
        out[name] = [out[name]]
    for name, value in out.items(): check(value, SCHEMA['UserAll'][name], name)
    return dict(userId=user_id,loginId=login_id,loginDateTime=login_date_time,
                capturedAt=captured_at,complete=complete,itemKinds=deepcopy(item_kinds),
                nodes=out,rawFavoriteKinds=raw_kinds)
