"""Private ticket routing and fail-closed orchestration without network."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import qr_sync as q


class TicketCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.private = Path(tmp.name)
        self.private.chmod(0o700)
        self.runner = Mock(return_value={'status': 'verified'})
        self.sync = q.QRSync(self.private, ticket_runner=self.runner)
        self.sent = []
        self.uid = '12345678'
    def event(self, text, typ='private'):
        return dict(post_type='message', message_type=typ, user_id=int(self.uid), raw_message=text)
    def send(self, uid, text): self.sent.append(text)
    def wait(self):
        self.assertTrue(self.sync.gate.acquire(timeout=2))
        self.sync.gate.release()
    def record(self):
        p = self.private / ('session-' + self.uid + '.json')
        p.write_text(json.dumps(dict(state='active', owner=self.uid)))
        p.chmod(0o600)
        plan = self.private / ('settlement-plan-' + self.uid + '-ticket-hold-2.json')
        plan.write_text(json.dumps(dict(owner=self.uid, ticket=dict(charge_id=2, action='hold'))))
        other = self.private / ('settlement-plan-' + self.uid + '-ticket-hold-3.json')
        other.write_text(json.dumps(dict(owner=self.uid, ticket=dict(charge_id=3, action='hold'))))
        other.chmod(0o600)
        plan.chmod(0o600)
    def test_targets_private_and_malformed(self):
        self.assertFalse(self.sync.handle(self.event('/购券 2', 'group'), self.send))
        self.assertTrue(q.group_sensitive(self.event('/购券 2', 'group')))
        for text in ('/购券', '/购券 4', '/购券 2 extra'):
            self.assertTrue(self.sync.handle(self.event(text), self.send))
        self.runner.assert_not_called()
    def test_session_required_and_gate_released(self):
        self.sync.handle(self.event('/购券 2'), self.send)
        self.wait()
        self.runner.assert_not_called()
        self.assertIn('未购券', self.sent[-1])
    def test_both_targets_route_and_sanitize(self):
        self.record()
        for target in (2, 3):
            self.sync.handle(self.event('/购券 ' + str(target)), self.send)
            self.wait()
            self.runner.assert_called_with(self.uid, target, private=self.private)
        self.runner.return_value = dict(status='ambiguous', reason='secret')
        self.sync.handle(self.event('/购券 2'), self.send)
        self.wait()
        self.assertIn('不要重复', self.sent[-1])
        self.assertNotIn('secret', self.sent[-1])
    def test_no_plan_never_launches_cli(self):
        with patch.object(q.subprocess, 'run') as run:
            with self.assertRaises(FileNotFoundError):
                q.ticket_purchase(self.uid, 2, private=self.private)
            run.assert_not_called()

    def test_result_distinguishes_consumed_and_retained(self):
        used = q.format_ticket_result(dict(status='verified', outcome='purchased_and_consumed_in_play'), 2)
        held = q.format_ticket_result(dict(status='verified', outcome='inventory_retained'), 3)
        self.assertIn('已在测试结算中消耗', used)
        self.assertIn('未执行用券游玩', held)
        self.assertIn('结算未发送', q.format_ticket_result(dict(status='purchase_verified_settlement_not_sent'), 2))

    def test_missing_plan_explicitly_says_no_request(self):
        self.record()
        (self.private / ('settlement-plan-' + self.uid + '-ticket-hold-2.json')).unlink()
        self.sync.handle(self.event('/购券 2'), self.send)
        self.wait()
        self.runner.assert_not_called()
        self.assertIn('未发送', self.sent[-1])

    def test_prepare_plan_from_declared_simulation(self):
        profile = self.private / 'ticket-machine-profile.json'
        profile.write_text(json.dumps(dict(machine={'placeId': 7}, extend_flags={'isPhotoAgree': False, 'isGotoCodeRead': False})))
        profile.chmod(0o600)
        result = q.prepare_ticket_plan(self.uid, 3, self.private)
        self.assertEqual(result['auth_date_time'], 'session')
        self.assertEqual(result['ticket'], dict(charge_id=3, action='hold'))
        self.assertEqual(result['item_ids'], [])
        plan = self.private / ('settlement-plan-' + self.uid + '-ticket-hold-3.json')
        self.assertEqual(plan.stat().st_mode & 0o777, 0o600)
        profile.unlink()
        self.assertEqual(q.prepare_ticket_plan(self.uid, 3, self.private), result)

    def test_cli_command_targets_plan_and_preserves_session_lock(self):
        import subprocess
        self.record()
        def run(argv, **kwargs):
            self.assertIn('--ticket-purchase', argv)
            self.assertNotIn('--buy-ticket', argv)
            self.assertIn(str(self.private / ('settlement-plan-' + self.uid + '-ticket-hold-2.json')), argv)
            self.assertEqual(kwargs['pass_fds'], (q._session_local.fd,))
            return subprocess.CompletedProcess(argv, 0, json.dumps(dict(status='verified', outcome='purchased_and_consumed_in_play')).encode(), b'')
        with q.session_lock(self.private), patch.object(q.subprocess, 'run', side_effect=run):
            result = q.ticket_purchase(self.uid, 2, private=self.private)
        self.assertEqual(result['status'], 'verified')

    def test_legacy_buy_use_plan_cannot_start_purchase(self):
        self.record()
        plan = self.private / ('settlement-plan-' + self.uid + '-ticket-hold-2.json')
        plan.write_text(json.dumps(dict(owner=self.uid, ticket=dict(charge_id=2, action='buy_use'))))
        with patch.object(q.subprocess, 'run') as run:
            with self.assertRaises(q.CaptureError):
                q.ticket_purchase(self.uid, 2, private=self.private)
            run.assert_not_called()


class GroupTicketRouteTests(unittest.TestCase):
    def test_group_ticket_routes_without_live_purchase(self):
        import main
        for text in ('/购券 2', '/购券 3', '/购券'):
            event = dict(post_type='message', message_type='group', group_id=123456789,
                         user_id=12345678, raw_message=text,
                         message=[{'type':'text','data':{'text':text}}])
            with patch.object(main, 'ALLOWED_GROUP_IDS', frozenset({123456789})), \
                 patch.object(q.DEFAULT_SYNC, 'handle') as handle, \
                 patch('site_bindings.handle', return_value=False), \
                 patch.object(main, '_group_qr_hint') as reply:
                main.handle(event)
                handle.assert_called_once()
                forwarded, send = handle.call_args.args
                self.assertEqual(forwarded['message_type'], 'private')
                self.assertEqual(forwarded['user_id'], 12345678)
                self.assertEqual(forwarded['raw_message'], text)
                send(12345678, '测试结果')
                reply.assert_called_once_with(123456789, '测试结果')
