"""Explicit 2x/3x charge purchase and optional one-play consumption.

Caller holds the existing session lock from collection through completion. No
login, QR, config or network at import time. A shared permanent settlement claim
covers BOTH writes, so a crash/timeout cannot be retried through either CLI.
This records a charge purchase; it neither performs nor attests to payment.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import time

from basic_settlement import build_basic_settlement, check, required
from settlement_submit import _claim, _claimed, _validate, _readback

CST = timezone(timedelta(hours=8))
CHARGE_KEYS = {'chargeId', 'stock', 'purchaseDate', 'validDate'}


def charges(rows):
    check(rows, {'array': 'UserCharge'}, 'userChargeList')
    seen = set()
    for row in rows:
        if row['chargeId'] <= 0 or row['stock'] < 0 or row['chargeId'] in seen:
            raise ValueError('invalid_charge_identity_or_stock')
        seen.add(row['chargeId'])
    return deepcopy(rows)


def _date(text):
    if type(text) is not str:
        raise ValueError('invalid_charge_date')
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CST)
        except ValueError:
            pass
    raise ValueError('invalid_charge_date')


def read_charges(client, session, current):
    current()
    reply = client._game('GetUserChargeApi', {'userId': session.user_id}, session.user_agent_identity)
    current()
    if type(reply) is not dict or required(reply, 'userId', int) != session.user_id:
        raise ValueError('charge_readback_binding')
    if 'returnCode' in reply and required(reply, 'returnCode', int) != 1:
        raise ValueError('charge_readback_code')
    if 'nextIndex' in reply and required(reply, 'nextIndex', int) != 0:
        raise ValueError('charge_continuation')
    length = required(reply, 'length', int)
    rows = reply.get('userChargeList')
    if rows is None and length == 0:
        rows = []
    if type(rows) is not list or length != len(rows) or length > 4096:
        raise ValueError('charge_readback_length')
    # Some GET implementations expose extNum1; it is not in UserCharge VO.
    normalized = []
    for row in rows:
        if type(row) is not dict or set(row) - CHARGE_KEYS - {'extNum1'}:
            raise ValueError('charge_readback_fields')
        if 'extNum1' in row and (type(row['extNum1']) is not int or row['extNum1'] != 0):
            raise ValueError('unsupported_charge_extension')
        normalized.append({key: row[key] for key in CHARGE_KEYS})
    return charges(normalized)


def _same(actual, expected):
    # GET dates may omit the fractional suffix; compare wall times, not strings.
    def canonical(rows):
        return {r['chargeId']: (r['stock'], _date(r['purchaseDate']), _date(r['validDate'])) for r in rows}
    return canonical(actual) == canonical(expected)


def build_ticket_purchase(snapshot, machine, offerings, *, charge_id, now_unix):
    """Return exact four-field UserChargelogRequestVO plus post-buy inventory.

    userChargelog.price is ticket.creditNum (1/2), NOT gameCharge.price. The
    offering price is cabinet credit cost and is only used for play accounting.
    Existing positive charge stock is rejected: native paid selection always
    buys; it does not provide a proven use-existing-stock paid-ticket path.
    """
    if type(charge_id) is not int or charge_id not in (2, 3):
        raise ValueError('only_2x_3x_supported')
    if type(now_unix) is not int or now_unix <= 0:
        raise ValueError('invalid_purchase_clock')
    rows = charges(required(required(snapshot, 'nodes', dict), 'userChargeList', list))
    if any(r['stock'] > 0 for r in rows):
        raise ValueError('existing_stock_requires_manual_reconciliation')
    if type(offerings) is not dict:
        raise ValueError('invalid_offerings')
    if 'returnCode' in offerings and required(offerings, 'returnCode', int) != 1:
        raise ValueError('offerings_rejected')
    offers = required(offerings, 'gameChargeList', list)
    if required(offerings, 'length', int) != len(offers) or len(offers) > 4096:
        raise ValueError('invalid_offerings_length')
    seen, selected = set(), None
    now_cst = datetime.fromtimestamp(now_unix, CST)
    for offer in offers:
        cid = required(offer, 'chargeId', int)
        if cid in seen:
            raise ValueError('duplicate_offering')
        seen.add(cid)
        if cid == charge_id:
            price = required(offer, 'price', int)
            if not 0 <= price <= 2147483647:
                raise ValueError('invalid_offering_price')
            if not _date(required(offer, 'startDate', str)) <= now_cst <= _date(required(offer, 'endDate', str)):
                raise ValueError('offering_not_active')
            selected = offer
    if selected is None:
        raise ValueError('ticket_not_offered')
    # Native TimeManager always formats purchase strings in China time (.f).
    purchase = now_cst.strftime('%Y-%m-%d %H:%M:%S') + '.0'
    valid = (now_cst - timedelta(hours=4) + timedelta(days=90)).strftime('%Y-%m-%d') + ' 04:00:00'
    row = dict(chargeId=charge_id, stock=1, purchaseDate=purchase, validDate=valid)
    rows = [r for r in rows if r['chargeId'] != charge_id] + [row]
    profile, = required(snapshot['nodes'], 'userData', list)
    log = dict(chargeId=charge_id, price=charge_id-1, purchaseDate=purchase,
        playCount=required(profile, 'playCount', int), playerRating=required(profile, 'playerRating', int),
        placeId=required(machine, 'placeId', int), regionId=required(machine, 'regionId', int),
        clientId=required(machine, 'clientId', str))
    for key in ('playCount', 'playerRating', 'placeId', 'regionId'):
        check(log[key], 'int', 'userChargelog.' + key)
        if log[key] < 0:
            raise ValueError('negative_chargelog_value')
    if not log['clientId']:
        raise ValueError('missing_client_identity')
    packet = dict(userId=required(snapshot, 'userId', int), userChargelog=log,
                  userCharge=deepcopy(row), loginDateTime=required(snapshot, 'loginDateTime', int))
    return packet, rows, selected['price']


def validate_hold(snapshot, machine, binding, now):
    for key, value in zip(('userId', 'loginId', 'loginDateTime'), binding[:3]):
        if required(snapshot, key, int) != value:
            raise ValueError('purchase_session_mismatch')
    age = now - required(snapshot, 'capturedAt', int)
    if not 0 <= age <= 300:
        raise ValueError('stale_purchase_snapshot')
    for key in ('placeId', 'regionId'):
        if required(machine, key, int) < 0:
            raise ValueError('invalid_machine_identity')
    if not required(machine, 'clientId', str):
        raise ValueError('invalid_machine_identity')
    charges(required(required(snapshot, 'nodes', dict), 'userChargeList', list))


def collect_hold_snapshot(client, session, *, captured_at):
    """Purchase needs profile counters and inventory, never a play snapshot."""
    client._current(session)
    reply = client._game('GetUserDataApi', {'userId': session.user_id}, session.user_agent_identity)
    client._current(session)
    if required(reply, 'userId', int) != session.user_id:
        raise ValueError('purchase_profile_account_mismatch')
    if 'returnCode' in reply and required(reply, 'returnCode', int) != 1:
        raise ValueError('purchase_profile_rejected')
    profile = required(reply, 'userData', dict)
    for key in ('playCount', 'playerRating'):
        if required(profile, key, int) < 0:
            raise ValueError('invalid_profile_counter')
    rows = read_charges(client, session, lambda: client._current(session))
    return dict(userId=session.user_id, loginId=session.login_id,
        loginDateTime=session.login_date_time, capturedAt=captured_at,
        nodes=dict(userData=[deepcopy(profile)], userChargeList=rows))


def submit_ticket(client, session, *, owner, private, snapshot, machine, build_args,
                  charge_id, action, now=time.time):
    """Buy-and-hold or buy-and-use, single attempt, with authoritative reads.

    action='hold': purchase only, inventory +1, no play submitted.
    action='buy_use': purchase -> callback 1 AND matching inventory -> prepared
    all-miss play with useTicketId and stock -1. Both packets are fully built
    before claiming/sending. item_ids must explicitly be [] in build_args.
    A timeout/negative callback never advances to settlement or retries.
    """
    try:
        if action not in ('hold', 'buy_use') or type(owner) is not str or not owner.strip():
            raise ValueError('invalid_ticket_action_or_owner')
        binding = tuple(getattr(session, k) for k in
                        ('user_id', 'login_id', 'login_date_time', 'user_agent_identity'))
        if any(type(v) is not int or v <= 0 for v in binding[:3]) or type(binding[3]) is not str:
            raise ValueError('invalid_session')
        def current():
            client._current(session)
            if binding != tuple(getattr(session, k) for k in
                                ('user_id', 'login_id', 'login_date_time', 'user_agent_identity')):
                raise ValueError('session_changed')
        current()
        if _claimed(private, binding, owner):
            return dict(status='rejected', reason='already_claimed', retry_allowed=False)
        snapshot, machine, args = deepcopy(snapshot), deepcopy(machine), deepcopy(build_args)
        if args.get('item_ids') != [] or type(args.get('item_ids')) is not list:
            raise ValueError('ticket_flow_requires_no_cosmetic_delta')
        args['now_unix'] = int(now())
        # Validate the entire baseline and explicit play/machine evidence BEFORE
        # any purchase, even for hold. This also enforces freshness and binding.
        if action == 'buy_use':
            baseline = build_basic_settlement(snapshot, machine, **args)
            _validate(baseline, snapshot, machine, binding, now())
        else:
            validate_hold(snapshot, machine, binding, now())
            baseline = {}
        current()
        offers = client._game('GetGameChargeApi', {'isAll': False}, binding[3])
        current()
        purchase, after_buy, cost = build_ticket_purchase(snapshot, machine, offers,
            charge_id=charge_id, now_unix=int(now()))
        final = deepcopy(baseline)
        final_rows = deepcopy(after_buy)
        if action == 'buy_use':
            next(r for r in final_rows if r['chargeId'] == charge_id)['stock'] -= 1
            final['upsertUserAll']['userChargeList'] = final_rows
            # ExportUserGamePlaylog: base game credit + GetTicketCredit (offer).
            credit = required(machine, 'playCredit', int) + cost
            check(credit, 'int', 'ticketPlayCredit')
            game_log = final['upsertUserAll']['userGamePlaylogList'][0]
            game_log.update(useTicketId=charge_id, playCredit=credit)
            # ResultProcess adds GetBoughtTicketId credit to LastPlayCredit too.
            final['upsertUserAll']['userData'][0]['lastPlayCredit'] = credit
            # LastSelectTicket is UI state; preserve the explicit baseline.
            _validate(final, snapshot, machine, binding, now())
        if not _same(read_charges(client, session, current), snapshot['nodes']['userChargeList']):
            raise ValueError('inventory_changed_since_snapshot')
    except Exception:
        return dict(status='rejected', reason='invalid_or_stale_ticket_input', retry_allowed=False)
    try:
        if not _claim(private, binding, owner):
            return dict(status='rejected', reason='already_claimed', retry_allowed=False)
    except Exception:
        return dict(status='rejected', reason='claim_failed', retry_allowed=False)
    try:
        current()
        if action == 'buy_use':
            _validate(baseline, snapshot, machine, binding, now())
        else:
            validate_hold(snapshot, machine, binding, now())
    except Exception:
        return dict(status='rejected', reason='pre_send_validation_failed', retry_allowed=False)

    def send(api, body):
        try:
            current()
            reply = client._game(api, deepcopy(body), binding[3])
            current()
            return (type(reply) is dict and type(reply.get('returnCode')) is int and
                    reply['returnCode'] == 1 and ('userId' not in reply or
                    type(reply['userId']) is int and reply['userId'] == binding[0]))
        except Exception:
            return False

    acknowledged = send('UpsertUserChargelogApi', purchase)
    bought = False
    try:
        bought = _same(read_charges(client, session, current), after_buy)
    except Exception:
        pass
    result = dict(status='ambiguous', stage='purchase', purchase_acknowledged=acknowledged,
                  checks=dict(purchase_inventory=bought), retry_allowed=False, payment='not_attested')
    if not acknowledged:
        return result
    result['status'] = 'acknowledged_unverified'
    if not bought:
        return result
    if action == 'hold':
        result.update(status='verified', outcome='inventory_retained')
        return result
    try:
        # Expiry of the original snapshot while buying must stop the next write.
        current()
        _validate(final, snapshot, machine, binding, now())
    except Exception:
        result.update(status='purchase_verified_settlement_not_sent', reason='stale_or_changed_session')
        return result
    settled = send('UpsertUserAllApi', final)
    checks = _readback(client, session, final, snapshot, args.get('kind', 1), current)
    checks['purchase_inventory'] = bought
    try:
        checks['final_inventory'] = _same(read_charges(client, session, current), final_rows)
    except Exception:
        checks['final_inventory'] = False
    result.update(stage='settlement', settlement_acknowledged=settled, checks=checks,
                  status=('verified' if all(checks.values()) else 'acknowledged_unverified') if settled else 'ambiguous')
    if result['status'] == 'verified':
        result['outcome'] = 'purchased_and_consumed_in_play'
    return result
