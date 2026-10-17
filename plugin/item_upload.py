"""Pure item delta preparation, based on the supplied game's ExportUserAll IL.

No network or session access. A prepared full request is required; a GetUserData
response alone is NOT an UpsertUserAll request. Never invent missing save nodes.
"""
from copy import deepcopy

KINDS = {'背景板': 1, '称号': 2, '头像': 3, '搭档': 10, '姓名框': 11}
FIELDS = frozenset(('itemKind', 'itemId', 'stock', 'isValid'))


def validate_items(rows):
    if type(rows) is not list or len(rows) > 100000:
        raise ValueError('道具列表格式无效')
    seen = set()
    for row in rows:
        if type(row) is not dict or set(row) != FIELDS:
            raise ValueError('道具字段不完整或含未知字段')
        for key in ('itemKind', 'itemId', 'stock'):
            if type(row[key]) is not int or not 0 <= row[key] <= 2147483647:
                raise ValueError('道具数值无效')
        if type(row['isValid']) is not bool:
            raise ValueError('道具有效状态无效')
        key = (row['itemKind'], row['itemId'])
        if key in seen:
            raise ValueError('道具标识重复')
        seen.add(key)
    return seen


def item_delta(baseline, current):
    """Equivalent to BuildListData's Except + identity comparison for UserItem.

    Baseline is the complete downloaded inventory, current the desired inventory.
    Unchanged structs are omitted; changed existing identities use bit 0, new 1.
    This is a delta, not a replacement inventory and does not encode deletions.
    """
    old_keys = validate_items(baseline)
    validate_items(current)
    old = {(r['itemKind'], r['itemId']): r for r in baseline}
    changed = [deepcopy(r) for r in current
               if old.get((r['itemKind'], r['itemId'])) != r]
    return {'userItemList': changed,
            'isNewItemList': ''.join('0' if (r['itemKind'], r['itemId']) in old_keys
                                     else '1' for r in changed)}


def add_collections(baseline, kind, ids):
    """Add cosmetic collections only; preserve all existing rows and stock.

    Existing valid owned collections are no-ops. Mirrors AddCollections' stock=0
    branch without turning cosmetic additions into consumable stock increments.
    """
    validate_items(baseline)
    if type(kind) is not int or kind not in KINDS.values():
        raise ValueError('仅支持背景板、称号、头像、搭档、姓名框')
    if type(ids) is not list or not 1 <= len(ids) <= 100:
        raise ValueError('每次提供 1 至 100 个道具 ID')
    if any(type(i) is not int or not 1 <= i <= 2147483647 for i in ids):
        raise ValueError('道具 ID 无效')
    if len(set(ids)) != len(ids):
        raise ValueError('道具 ID 重复')
    current = deepcopy(baseline)
    by_key = {(r['itemKind'], r['itemId']): r for r in current}
    for item_id in ids:
        row = by_key.get((kind, item_id))
        if row is None:
            current.append({'itemKind': kind, 'itemId': item_id,
                            'stock': 1, 'isValid': True})
        elif not row['isValid']:
            raise ValueError('已有道具被标记无效，不能自动解除限制')
        elif row['stock'] == 0:
            row['stock'] = 1
    return current


def merge_item_delta(prepared_request, baseline, current):
    """Preserve a caller-prepared complete envelope; never construct one here.

    This validates local merge invariants ONLY, not server readiness. The caller
    must separately prove session ownership, freshness and full request schema.
    Existing item changes are rejected rather than silently overwritten.
    """
    if type(prepared_request) is not dict:
        raise ValueError('缺少完整上传请求')
    for key in ('userId', 'playlogId', 'loginDateTime'):
        if type(prepared_request.get(key)) is not int or prepared_request[key] <= 0:
            raise ValueError('缺少登录会话标识')
    for key in ('isEventMode', 'isFreePlay'):
        if type(prepared_request.get(key)) is not bool:
            raise ValueError('缺少游戏模式字段')
    if type(prepared_request.get('userPlaylogList')) is not list:
        raise ValueError('缺少游玩记录字段')
    ua = prepared_request.get('upsertUserAll')
    if type(ua) is not dict or type(ua.get('userData')) is not list or len(ua['userData']) != 1 or type(ua['userData'][0]) is not dict or not ua['userData'][0]:
        raise ValueError('缺少完整存档数据，不能填空上传')
    if ua.get('userItemList') != [] or ua.get('isNewItemList') != '':
        raise ValueError('请求已有道具变更或缺少道具字段，不能覆盖')
    result = deepcopy(prepared_request)
    result['upsertUserAll'].update(item_delta(baseline, current))
    return result



def attach_to_settlement(prepared_request, baseline, kind, ids, *,
                         user_id, login_id, login_date_time):
    """Attach cosmetics to caller-supplied gameplay settlement, without inventing it.

    Checks binding and merge invariants, NOT the full version-specific gameplay
    schema. Caller must provide a validated fresh settlement and complete inventory
    for every kind present in its pending item changes and the requested kind.
    """
    expected = {'userId': user_id, 'playlogId': login_id, 'loginDateTime': login_date_time}
    if type(prepared_request) is not dict or any(
            type(v) is not int or v <= 0 or type(prepared_request.get(k)) is not int
            or prepared_request[k] != v for k, v in expected.items()):
        raise ValueError('结算与当前登录会话不匹配')
    logs = prepared_request.get('userPlaylogList')
    ua = prepared_request.get('upsertUserAll')
    if type(logs) is not list or not logs or any(type(r) is not dict or not r for r in logs):
        raise ValueError('缺少本次游玩记录，不能生成虚构结算')
    if type(ua) is not dict:
        raise ValueError('缺少结算存档')
    for name in ('userData', 'userExtend', 'userOption', 'userRatingList'):
        value = ua.get(name)
        if type(value) is not list or len(value) != 1 or type(value[0]) is not dict or not value[0]:
            raise ValueError('缺少结算资料节点')
    game_logs = ua.get('userGamePlaylogList')
    if type(game_logs) is not list or not game_logs or any(
            type(r) is not dict or type(r.get('playlogId')) is not int
            or r['playlogId'] != login_id for r in game_logs):
        raise ValueError('结算日志与登录会话不匹配')
    if type(ua.get('userMusicDetailList')) is not list or not ua['userMusicDetailList']:
        raise ValueError('缺少本次结算成绩')
    old_keys = validate_items(baseline)
    pending = ua.get('userItemList')
    validate_items(pending)
    bits = ua.get('isNewItemList')
    expected_bits = ''.join('0' if (r['itemKind'], r['itemId']) in old_keys else '1' for r in pending)
    if type(bits) is not str or bits != expected_bits:
        raise ValueError('已有道具差量与基线不一致')
    current = {(r['itemKind'], r['itemId']): deepcopy(r) for r in baseline}
    for row in pending:
        current[(row['itemKind'], row['itemId'])] = deepcopy(row)
    desired = add_collections(list(current.values()), kind, ids)
    clean = deepcopy(prepared_request)
    clean['upsertUserAll']['userItemList'] = []
    clean['upsertUserAll']['isNewItemList'] = ''
    return merge_item_delta(clean, baseline, desired)


def main(argv=None):
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description='道具离线差量预览；不会发送上传请求')
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--kind', choices=KINDS, required=True)
    parser.add_argument('--ids', type=int, nargs='+', required=True)
    args = parser.parse_args(argv)
    try:
        with args.baseline.open('rb') as f:
            raw = f.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('基线文件过大')
        def unique_pairs(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError('JSON 字段重复')
                obj[key] = value
            return obj
        baseline = json.loads(raw, object_pairs_hook=unique_pairs)
        current = add_collections(baseline, KINDS[args.kind], args.ids)
        result = item_delta(baseline, current)
    except (OSError, ValueError, TypeError):
        print(json.dumps({'status': 'rejected', 'reason': '基线或道具参数无效'}, ensure_ascii=False))
        return 2
    print(json.dumps({'status': 'offline_preview', 'requests_live': 0,
                      'changed': len(result['userItemList']), 'delta': result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
