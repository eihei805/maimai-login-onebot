"""Private ticket CLI orchestration, synthetic retained session only."""
import contextlib
import io
import json
import sys
import unittest
from unittest.mock import Mock, patch
from test_ticket_cli import TicketCLITests, live, USER


class PurchaseCLITests(unittest.TestCase):
    def setUp(self):
        TicketCLITests.setUp(self)
        self.path_plan = self.private / 'settlement-plan-ticket.json'
        self.plan = dict(owner=USER, machine=dict(utcOffsetSeconds=28800,
            clientId=self.config.chip_id, placeId=2, regionId=1),
            extend_flags=dict(isPhotoAgree=False, isGotoCodeRead=False),
            auth_date_time=1000, kind=1, item_ids=[], ticket=dict(charge_id=2, action='buy_use'))

    unchanged = TicketCLITests.unchanged

    def invoke(self, extra=None):
        self.path_plan.write_text(json.dumps(self.plan)); self.path_plan.chmod(0o600)
        collector = Mock(return_value={'synthetic': True})
        submit = Mock(return_value={'status': 'verified'})
        output = io.StringIO()
        original_transport = live.Transport
        transports = []
        def transport(*args, **kw):
            obj = original_transport(*args, **kw); transports.append(obj); return obj
        with patch.object(live, 'load_config', return_value=(self.config, dict(place_id=2,region_id=1))), \
             patch.object(live, 'Transport', side_effect=transport), \
             patch('settlement_collect.collect_snapshot', collector), \
             patch('ticket_settlement.collect_hold_snapshot', collector), \
             patch('ticket_settlement.submit_ticket', submit), \
             patch.object(sys, 'argv', ['client','--buy-ticket','--session-owner',USER,
                                      '--settlement-plan',str(self.path_plan)] + (extra or [])), \
             contextlib.redirect_stdout(output):
            code = live.main()
        return code, json.loads(output.getvalue()), collector, submit, transports

    def test_buy_use_restores_collects_and_submits_under_existing_lock(self):
        code, out, collect, submit, transports = self.invoke()
        self.assertEqual(code, 0); self.assertEqual(out['status'], 'verified')
        collect.assert_called_once(); submit.assert_called_once()
        kw = submit.call_args.kwargs
        self.assertEqual(kw['charge_id'], 2); self.assertEqual(kw['action'], 'buy_use')
        self.assertEqual(kw['build_args']['item_ids'], [])
        self.assertEqual(kw['build_args']['user_id'], 123)
        self.assertTrue(transports[0].allow_ticket_purchase)
        self.assertTrue(transports[0].allow_settlement)
        self.unchanged()

    def test_session_auth_timestamp_used_without_guessing(self):
        self.plan['auth_date_time'] = 'session'
        data = json.loads(self.path.read_text())
        data['auth_date_time'] = 999
        self.path.write_text(json.dumps(data))
        self.before = self.path.read_bytes()
        self.before_mtime = self.path.stat().st_mtime_ns
        _, _, _, submit, _ = self.invoke()
        self.assertEqual(submit.call_args.kwargs['build_args']['auth_date_time'], 999)
        self.unchanged()

    def test_old_session_without_auth_time_stops_before_collection(self):
        self.plan['auth_date_time'] = 'session'
        with self.assertRaisesRegex(live.SafeFailure, 'session_auth_time_unavailable'):
            self.invoke()
        self.unchanged()

    def test_hold_never_authorizes_settlement_transport(self):
        self.plan['ticket'] = dict(charge_id=3, action='hold')
        _, _, _, submit, transports = self.invoke()
        self.assertEqual(submit.call_args.kwargs['charge_id'], 3)
        self.assertFalse(transports[0].allow_settlement)
        self.assertTrue(transports[0].allow_ticket_purchase)
        self.unchanged()

    def test_qq_charge_argument_defaults_to_stock_retained(self):
        del self.plan['ticket']
        del self.plan['kind']
        del self.plan['item_ids']
        _, _, _, submit, transports = self.invoke(['--charge-id','3'])
        self.assertEqual(submit.call_args.kwargs['charge_id'],3)
        self.assertEqual(submit.call_args.kwargs['action'],'hold')
        self.assertFalse(transports[0].allow_settlement)

    def test_auth_session_resolves_exact_persisted_request_epoch(self):
        self.plan['auth_date_time'] = 'session'
        record = live.read_session(USER)
        record['auth_date_time'] = 777
        live.write_session(USER, record)
        _, _, _, submit, _ = self.invoke()
        self.assertEqual(submit.call_args.kwargs['build_args']['auth_date_time'],777)
        self.assertNotEqual(submit.call_args.kwargs['build_args']['login_date_time'],777)

    def test_legacy_session_never_guesses_auth_epoch(self):
        self.plan['auth_date_time'] = 'session'
        with self.assertRaisesRegex(live.SafeFailure,'session_auth_time_unavailable'):
            self.invoke()

    def test_auth_epoch_survives_persist_restore_without_login_time_guess(self):
        import dataclasses
        from unittest.mock import Mock
        client = Mock()
        client.config = self.config
        transport = live.Transport(self.config)
        live.restore_session(USER, client, transport)
        client.auth_date_time = 777
        live.persist_session(USER, client, transport)
        self.assertEqual(live.read_session(USER)['auth_date_time'],777)
        restored = Mock(); restored.config = self.config
        live.restore_session(USER,restored,live.Transport(self.config))
        self.assertEqual(restored.auth_date_time,777)
        self.assertEqual(restored.session.login_date_time,1000)

    def test_explicit_qq_buy_use(self):
        del self.plan['ticket']
        _, _, _, submit, _ = self.invoke(['--charge-id','2','--ticket-action','buy_use'])
        self.assertEqual(submit.call_args.kwargs['action'],'buy_use')

    def test_bad_machine_and_target_fail_before_collection(self):
        for key, value in [('utcOffsetSeconds',0),('clientId','other'),('placeId',9)]:
            original = self.plan['machine'][key]; self.plan['machine'][key] = value
            with self.assertRaises(live.SafeFailure): self.invoke()
            self.plan['machine'][key] = original
        self.plan['ticket']['charge_id'] = True
        with self.assertRaises(live.SafeFailure): self.invoke()
        self.unchanged()


if __name__ == '__main__': unittest.main()
