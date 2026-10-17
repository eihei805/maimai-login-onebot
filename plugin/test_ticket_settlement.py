"""Offline purchase/coordinator tests. All requests use synthetic callbacks."""
import copy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import ticket_settlement as ticket
from test_basic_settlement import BuilderTests
from test_settlement_submit import FakeClient
from test_settlement_transport import SettlementTransportTests
from test_ticket_cli import live, m


class TicketClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.charges = []
        self.buy_code = 1
        self.buy_timeout = False
        self.bad_buy_read = False
        self.offers = dict(length=2, gameChargeList=[dict(chargeId=cid, price=cid+5,
            startDate='2020-01-01 00:00:00', endDate='2099-01-01 00:00:00') for cid in (2, 3)])

    def _game(self, api, body, identity):
        if api in ('GetGameChargeApi', 'GetUserChargeApi', 'UpsertUserChargelogApi'):
            self.calls.append((api, copy.deepcopy(body)))
        if api == 'GetGameChargeApi':
            assert body == {'isAll': False}
            return copy.deepcopy(self.offers)
        if api == 'GetUserChargeApi':
            assert body == {'userId': 1}
            rows = copy.deepcopy(self.charges)
            if self.bad_buy_read and rows:
                rows[0]['stock'] += 1
            return dict(userId=1, length=len(rows), userChargeList=rows)
        if api == 'UpsertUserChargelogApi':
            self.charges = [copy.deepcopy(body['userCharge'])]
            if self.buy_timeout:
                raise TimeoutError('private credential')
            return dict(returnCode=self.buy_code)
        reply = super()._game(api, body, identity)
        if api == 'UpsertUserAllApi':
            self.charges = copy.deepcopy(body['upsertUserAll']['userChargeList'])
        return reply


class TicketTests(unittest.TestCase):
    def setUp(self):
        fixture = BuilderTests(); fixture.setUp()
        self.snapshot, self.machine, self.args = fixture.snap, fixture.machine, fixture.kw
        self.args['item_ids'] = []
        self.session = SimpleNamespace(user_id=1, login_id=2, login_date_time=1800000000,
                                       user_agent_identity='offline')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.client = TicketClient()
        self.charge = 2
        self.action = 'buy_use'

    def call(self):
        return ticket.submit_ticket(self.client, self.session, owner='owner', private=self.tmp.name,
            snapshot=self.snapshot, machine=self.machine, build_args=self.args,
            charge_id=self.charge, action=self.action, now=lambda: 1800000040)

    def writes(self):
        return [api for api, _ in self.client.calls if api.startswith('Upsert')]

    def test_both_multipliers_purchase_then_consumption(self):
        for cid in (2, 3):
            with self.subTest(cid=cid), tempfile.TemporaryDirectory() as directory:
                self.tmp.name = directory; self.client = TicketClient(); self.charge = cid
                original = copy.deepcopy((self.snapshot, self.machine, self.args))
                out = self.call()
                self.assertEqual(out['status'], 'verified')
                self.assertEqual(out['outcome'], 'purchased_and_consumed_in_play')
                self.assertEqual(self.writes(), ['UpsertUserChargelogApi', 'UpsertUserAllApi'])
                buy = next(body for api, body in self.client.calls if api == 'UpsertUserChargelogApi')
                self.assertEqual(set(buy), {'userId', 'userChargelog', 'userCharge', 'loginDateTime'})
                self.assertEqual(buy['userChargelog']['price'], cid-1)
                self.assertEqual(buy['userChargelog']['playCount'], 14)
                self.assertEqual(buy['userCharge']['stock'], 1)
                ua = self.client.packet['upsertUserAll']
                self.assertEqual(ua['userChargeList'][0]['stock'], 0)
                self.assertEqual(ua['userGamePlaylogList'][0]['useTicketId'], cid)
                self.assertEqual(ua['userGamePlaylogList'][0]['playCredit'], 1+cid+5)
                self.assertEqual(ua['userData'][0]['lastPlayCredit'], 1+cid+5)
                self.assertEqual(ua['userItemList'], [])
                self.assertEqual((self.snapshot, self.machine, self.args), original)
                self.assertEqual(self.call()['reason'], 'already_claimed')
                self.assertEqual(len(self.writes()), 2)

    def test_hold_does_not_submit_play(self):
        self.action = 'hold'
        for cid in (2, 3):
            with self.subTest(cid=cid), tempfile.TemporaryDirectory() as directory:
                self.tmp.name = directory
                self.client = TicketClient()
                self.charge = cid
                self.assertEqual(self.call()['outcome'], 'inventory_retained')
                self.assertEqual(self.writes(), ['UpsertUserChargelogApi'])
                self.assertEqual(self.client.charges[0]['chargeId'], cid)
                self.assertEqual(self.client.charges[0]['stock'], 1)

    def test_purchase_timeout_even_with_matching_readback_never_settles_or_retries(self):
        self.client.buy_timeout = True
        out = self.call()
        self.assertEqual(out['status'], 'ambiguous')
        self.assertTrue(out['checks']['purchase_inventory'])
        self.assertNotIn('private', str(out))
        self.assertEqual(self.call()['reason'], 'already_claimed')
        self.assertEqual(self.writes(), ['UpsertUserChargelogApi'])

    def test_only_integer_callback_one_advances(self):
        for code in (True, 0, 2, '1', None):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                self.tmp.name = directory; self.client = TicketClient(); self.client.buy_code = code
                self.assertEqual(self.call()['status'], 'ambiguous')
                self.assertEqual(self.writes(), ['UpsertUserChargelogApi'])

    def test_purchase_mismatch_stops_before_settlement(self):
        self.client.bad_buy_read = True
        self.assertEqual(self.call()['status'], 'acknowledged_unverified')
        self.assertEqual(self.writes(), ['UpsertUserChargelogApi'])

    def test_settlement_timeout_never_retries(self):
        self.client.timeout = True
        self.assertEqual(self.call()['status'], 'ambiguous')
        self.assertEqual(self.call()['reason'], 'already_claimed')
        self.assertEqual(self.writes(), ['UpsertUserChargelogApi', 'UpsertUserAllApi'])

    def test_unlisted_expired_duplicate_offers_no_write(self):
        for variant in ('unlisted', 'expired', 'duplicate'):
            self.client = TicketClient()
            if variant == 'unlisted': self.client.offers['gameChargeList'][0]['chargeId'] = 4
            if variant == 'expired': self.client.offers['gameChargeList'][0]['endDate'] = '2020-01-01 00:00:00'
            if variant == 'duplicate': self.client.offers['gameChargeList'][1]['chargeId'] = 2
            self.assertEqual(self.call()['status'], 'rejected')
            self.assertEqual(self.writes(), [])
            self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_existing_stock_and_changed_inventory_fail_before_claim(self):
        row = dict(chargeId=2, stock=1, purchaseDate='2026-01-01 00:00:00', validDate='2027-01-01 04:00:00')
        self.snapshot['nodes']['userChargeList'] = [row]
        self.assertEqual(self.call()['status'], 'rejected')
        self.snapshot['nodes']['userChargeList'] = []
        self.client.charges = [row]
        self.assertEqual(self.call()['status'], 'rejected')
        self.assertEqual(self.writes(), [])

    def test_global_claim_blocks_other_settlement_entry(self):
        self.action = 'hold'; self.assertEqual(self.call()['status'], 'verified')
        from settlement_submit import submit_basic
        out = submit_basic(self.client, self.session, owner='owner', private=self.tmp.name,
            snapshot=self.snapshot, machine=self.machine, build_args=self.args, now=lambda: 1800000040)
        self.assertEqual(out['reason'], 'already_claimed')

    def test_real_codec_and_transport_entire_flow_without_sockets(self):
        import hashlib
        from email.message import Message
        config_fixture = SettlementTransportTests(); config_fixture.setUp()
        config = config_fixture.config
        codec = m.Codec(config.aes_key, config.aes_iv)
        backend = self.client
        apis = ('GetGameChargeApi', 'GetUserChargeApi', 'UpsertUserChargelogApi',
                'UpsertUserAllApi', 'GetUserItemApi', 'GetUserDataApi', 'GetUserMusicApi')
        names = {hashlib.md5(api.encode()+b'MaimaiChn'+config.salt).hexdigest(): api for api in apis}
        class Opener:
            def open(inner, request, timeout):
                self.assertTrue(request.get_header('User-agent').endswith('#1'))
                api = names[request.full_url.rsplit('/', 1)[1]]
                body = m.object_json(codec.decode(request.data))
                reply = backend._game(api, body, 'offline')
                encoded = codec.encode(m.json_bytes(reply))
                class Response:
                    status = 200
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def info(self): return Message()
                    def read(self, limit): return encoded[:limit]
                return Response()
        transport = live.Transport(config, allow_settlement=True, allow_ticket_purchase=True)
        transport.identity = '1'; transport.opener = Opener()
        self.client = m.Client(config, transport); self.client.session = self.session
        with patch('socket.socket', side_effect=AssertionError('no sockets')):
            self.assertEqual(self.call()['status'], 'verified')
        self.assertEqual([a for a, _ in backend.calls if a.startswith('Upsert')],
                         ['UpsertUserChargelogApi', 'UpsertUserAllApi'])

    def test_snapshot_expires_during_purchase_no_second_write(self):
        original = self.client._game
        elapsed = [False]
        def game(api, body, identity):
            reply = original(api, body, identity)
            if api == 'UpsertUserChargelogApi': elapsed[0] = True
            return reply
        self.client._game = game
        out = ticket.submit_ticket(self.client, self.session, owner='owner', private=self.tmp.name,
            snapshot=self.snapshot, machine=self.machine, build_args=self.args,
            charge_id=2, action='buy_use', now=lambda: 1800000400 if elapsed[0] else 1800000040)
        self.assertEqual(out['status'], 'purchase_verified_settlement_not_sent')
        self.assertEqual(self.writes(), ['UpsertUserChargelogApi'])

    def test_date_boundary_and_credit_num(self):
        from datetime import datetime, timezone
        stamp = int(datetime(2026, 10, 3, 18, 30, tzinfo=timezone.utc).timestamp())
        p, _, cost = ticket.build_ticket_purchase(self.snapshot, self.machine, self.client.offers,
                                                  charge_id=2, now_unix=stamp)
        self.assertEqual(p['userCharge']['purchaseDate'], '2026-10-04 02:30:00.0')
        self.assertEqual(p['userCharge']['validDate'], '2027-01-01 04:00:00')
        self.assertEqual(p['userChargelog']['price'], 1)
        self.assertEqual(cost, 7)


class TicketTransportTests(SettlementTransportTests):
    def setUp(self):
        super().setUp()
        self.req = m.Request('UpsertUserChargelogApi', 'POST', self.config.game_base+'a'*32,
                             {'User-Agent':'test#123'}, b'synthetic')

    def test_timeout_consumes_transport_allowance(self):
        t = live.Transport(self.config, allow_ticket_purchase=True); t.identity='123'
        with patch.object(t.opener, 'open', side_effect=TimeoutError('secret')) as send:
            with self.assertRaises(live.SafeFailure): t(self.req)
            with self.assertRaisesRegex(live.SafeFailure, 'already_attempted'): t(self.req)
            self.assertEqual(send.call_count, 1)

    def test_success_cannot_be_repeated(self):
        from unittest.mock import Mock
        t = live.Transport(self.config, allow_ticket_purchase=True); t.identity='123'
        response=Mock(); response.status=200; response.read.return_value=b'ack'
        response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
        with patch.object(t.opener,'open',return_value=response) as send, patch.object(t.cookies,'extract_cookies'):
            self.assertEqual(t(self.req).body,b'ack')
            with self.assertRaises(live.SafeFailure): t(self.req)
            self.assertEqual(send.call_count,1)


if __name__ == '__main__':
    unittest.main()


class HoldCollectionTests(unittest.TestCase):
    def test_only_profile_inventory_and_no_play_builder(self):
        fixture = TicketTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        client, session = fixture.client, fixture.session
        original = client._game
        def game(api, body, identity):
            if api == 'GetUserDataApi':
                return dict(userId=1, userData=dict(playCount=13, playerRating=0))
            return original(api, body, identity)
        client._game = game
        snap = ticket.collect_hold_snapshot(client, session, captured_at=1800000040)
        with patch.object(ticket, 'build_basic_settlement', side_effect=AssertionError('no play')):
            out = ticket.submit_ticket(client, session, owner='owner', private=fixture.tmp.name,
                snapshot=snap, machine=fixture.machine, build_args={'item_ids': []},
                charge_id=2, action='hold', now=lambda: 1800000040)
        self.assertEqual(out['outcome'], 'inventory_retained')
        self.assertEqual([api for api, _ in client.calls if api.startswith('Upsert')],
                         ['UpsertUserChargelogApi'])
