"""Synthetic tests: never execute the live client or access a socket."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import main
import qr_sync as q

QR_NEW = 'SGWCMAID' + 'X' * 12 + 'B' * 64
QR = 'SGWCMAID' + 'X' * 12 + 'A' * 64
USER = '12345678'

class QRTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name)
        os.chmod(self.private, 0o700)
        self.sent = []
        self.result = q.CaptureResult(USER, self.private/'capture.json', [{'musicId':1}], {'status':'complete','rows':1,'logout':'unconfirmed','session':'active'})
        self.sync = q.QRSync(self.private, runner=Mock(return_value=self.result))
        network = patch('socket.socket', side_effect=AssertionError('offline'))
        network.start(); self.addCleanup(network.stop)
    def event(self, text, typ='private'):
        return {'post_type':'message','message_type':typ,'group_id':135990003,'user_id':int(USER),
                'raw_message':text, 'message':[{'type':'text','data':{'text':text}}]}
    def send(self, uid, text): self.sent.append((uid,text))
    def consent(self): self.sync.handle(self.event('/同意HTTP取分'), self.send)
    def wait(self):
        self.assertTrue(self.sync.gate.acquire(timeout=2)); self.sync.gate.release()
    def test_shape_and_group_suppression(self):
        self.assertTrue(q.valid_qr(QR)); self.assertFalse(q.valid_qr(QR[:-1]))
        self.assertFalse(self.sync.handle(self.event(QR, 'group'), self.send))
        with patch.object(main,'send_group') as group, patch.object(main,'send_private') as private_send, \
             patch.object(main,'onebot_api', return_value={'role':'member'}), \
             patch.object(main, 'ALLOWED_GROUP_IDS', frozenset({135990003})):
            for text in (QR, '/取分 '+QR, '/曲目 '+QR, '/绑定水鱼 secret'):
                main.handle(self.event(text,'group'))
            self.assertTrue(group.called); private_send.assert_not_called()
            self.assertTrue(all(QR not in call.args[1] and 'secret' not in call.args[1]
                                for call in group.call_args_list))
        self.sync.runner.assert_not_called()
    def test_consent_required_persistent_revoke(self):
        self.sync.handle(self.event(QR), self.send)
        self.assertIn('HTTP', self.sent[-1][1]); self.sync.runner.assert_not_called()
        self.consent()
        self.assertEqual((self.private/'qr-consents.json').stat().st_mode & 0o777, 0o600)
        self.assertTrue(q.QRSync(self.private)._consent(USER))
        self.sync.handle(self.event('/撤销HTTP取分'), self.send)
        self.assertFalse(self.sync._consent(USER))
        self.assertTrue(all(QR not in text for _,text in self.sent))
    def test_success_raw_and_command(self):
        self.consent()
        for text in (QR, '/取分 '+QR_NEW):
            self.sync.next_allowed.clear()
            self.sync.handle(self.event(text), self.send); self.wait()
        self.assertEqual(self.sync.runner.call_count,2)
        self.assertTrue(any('1 条成绩' in text and '14 分钟' in text for _,text in self.sent))
    def test_rejected_qr_no_session_claim_or_upload(self):
        self.result.summary = {'status': 'failed', 'rows': 0, 'logout': 'unconfirmed', 'login': 'not_started'}
        self.result.scores = []
        callback = Mock()
        self.sync.on_result = callback
        self.consent()
        self.sync.handle(self.event('/login ' + QR), self.send)
        self.wait()
        self.assertIn('二维码验证失败', self.sent[-1][1])
        self.assertNotIn('会话已保留', repr(self.sent))
        callback.assert_not_called()

    def test_busy_and_cooldown(self):
        self.consent(); started=threading.Event(); release=threading.Event()
        def runner(*args, **kwargs):
            started.set(); release.wait(2); return self.result
        self.sync.runner=runner
        self.sync.handle(self.event(QR), self.send)
        self.assertTrue(started.wait(1))
        self.sync.handle(self.event(QR), self.send)
        self.assertIn('正在进行', self.sent[-1][1])
        release.set(); self.wait()
        self.sync.handle(self.event(QR), self.send)
        self.assertIn('过于频繁', self.sent[-1][1])
    def test_malformed_and_nontext(self):
        self.consent()
        for text in ('/取分 bad', QR+'x', '/取分'):
            self.sync.handle(self.event(text), self.send)
        event=self.event(QR); event['message'].append({'type':'image','data':{}})
        self.sync.handle(event,self.send)
        self.sync.runner.assert_not_called()
    def test_failures_redacted_gate_released(self):
        self.consent(); self.sync.runner.side_effect=RuntimeError(QR+' token-secret')
        self.sync.handle(self.event(QR),self.send); self.wait()
        self.assertIn('登出状态未确认',self.sent[-1][1])
        self.assertNotIn(QR,repr(self.sent)); self.assertNotIn('token-secret',repr(self.sent))
    def test_partial_and_callback(self):
        self.consent(); callback=Mock(); self.sync.on_result=callback
        self.result.summary={'status':'partial','rows':1,'logout':'unconfirmed'}
        self.sync.handle(self.event(QR),self.send); self.wait()
        callback.assert_not_called(); self.assertIn('部分完成',self.sent[-1][1])
        self.result.summary['status']='complete'; self.sync.next_allowed.clear()
        self.sync.handle(self.event(QR_NEW),self.send); self.wait()
        callback.assert_called_once_with(self.result,self.send)
    def test_args_result_mode_and_timeout(self):
        def run(argv,**kwargs):
            self.assertNotIn(QR,repr(argv)); self.assertEqual(kwargs['input'],QR.encode())
            self.assertFalse(kwargs['shell']); self.assertEqual(kwargs['timeout'],210)
            for flag in ('--live','--qr-stdin','--allow-http-qr','--preload-profile','--session-owner'): self.assertIn(flag,argv)
            self.assertNotIn('--logout',argv)
            self.assertEqual(argv[argv.index('--session-owner')+1],USER)
            path=Path(argv[-1]); self.assertFalse(path.exists())
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f: json.dump({'summary':{'status':'complete','rows':1,'logout':'confirmed'},'scores':[{'musicId':1}]},f)
            return Mock(returncode=0)
        with patch.object(q.subprocess,'run',side_effect=run):
            a=q.capture(USER,QR,private=self.private); b=q.capture(USER,QR,private=self.private)
        self.assertNotEqual(a.path,b.path); self.assertEqual(a.path.stat().st_mode & 0o777,0o600)
        with patch.object(q.subprocess,'run',side_effect=subprocess.TimeoutExpired(['safe'],210)):
            with self.assertRaisesRegex(q.CaptureError,'timeout_logout_unknown'): q.capture(USER,QR,private=self.private)
    def test_unsafe_permissions_and_symlink(self):
        path=self.private/'x'; path.write_text('{}')
        with self.assertRaises(q.CaptureError): q.read_private(path)
        os.chmod(path,0o600); link=self.private/'link'; link.symlink_to(path)
        with self.assertRaises(OSError): q.read_private(link)
        os.chmod(self.private,0o755)
        self.sync.handle(self.event('/同意HTTP取分'),self.send)
        self.assertIn('暂不可用',self.sent[-1][1])
    def test_ws_dispatch_private_http_send(self):
        with patch.object(q,'DEFAULT_SYNC',self.sync), patch.object(main,'onebot_api') as api:
            main.dispatch_frame(json.dumps(self.event('/取分')))
            self.assertEqual(api.call_args.args[0],'send_private_msg')
            body=api.call_args.args[1]
            self.assertEqual(body['user_id'],int(USER)); self.assertNotIn('reply',repr(body))
        with patch.object(main,'http_json',return_value={'status':'ok','retcode':0,'data':{}}) as http, patch.object(main,'onebot_headers',return_value={'Authorization':'synthetic'}):
            main.send_private(int(USER),'safe')
            self.assertEqual(http.call_args.args[0],main.OB_BASE+'/send_private_msg')

class TicketInventoryTests(unittest.TestCase):
    """Read-only /查券 routing; the live client is always mocked."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name)
        os.chmod(self.private, 0o700)
        self.sent = []
        self.rows = [{'chargeId': 2, 'stock': 1, 'purchaseDate': '2026-10-13 12:00:00.0',
                      'validDate': '2027-01-11 04:00:00'}]
        self.inventory = Mock(return_value=self.rows)
        self.sync = q.QRSync(self.private, runner=Mock(), inventory_runner=self.inventory)
        network = patch('socket.socket', side_effect=AssertionError('offline'))
        network.start(); self.addCleanup(network.stop)

    def event(self, text):
        return {'post_type': 'message', 'message_type': 'private', 'group_id': 135990003,
                'user_id': int(USER), 'raw_message': text,
                'message': [{'type': 'text', 'data': {'text': text}}]}

    def send(self, uid, text): self.sent.append((uid, text))

    def record(self, state='active'):
        path = q.session_file(USER, self.private)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'owner': USER, 'version': 1, 'state': state,
                       'session': {'user_id': int(USER), 'login_id': 1, 'login_date_time': 1,
                                   'token': 'x', 'user_agent_identity': 'i'},
                       'inputs': {'region_id': 1, 'place_id': 1, 'user_agent_identity': 'i'},
                       'game_base': 'https://game.invalid/api/', 'chip_id': 'A63E01ZZZZZ',
                       'cookies': []}, f)

    def wait(self):
        self.assertTrue(self.sync.gate.acquire(timeout=2)); self.sync.gate.release()

    def test_recognised_as_sensitive(self):
        self.assertTrue(q.sensitive_text('/查券'))

    def test_requires_retained_session(self):
        self.assertTrue(self.sync.handle(self.event('/查券'), self.send)); self.wait()
        self.assertIn('/login', self.sent[-1][1]); self.inventory.assert_not_called()
        self.record(state='unknown')
        self.sync.handle(self.event('/查券'), self.send); self.wait()
        self.assertIn('无法查询', self.sent[-1][1]); self.inventory.assert_not_called()

    def test_read_only_summary_and_no_payment_claim(self):
        self.record()
        self.sync.handle(self.event('/查券'), self.send); self.wait()
        text = self.sent[-1][1]
        self.assertIn('2 倍券', text); self.assertIn('1 张', text)
        self.assertIn('不代表已付款', text)
        self.assertNotIn('到账成功', text)
        self.inventory.assert_called_once_with(USER, private=self.private)

    def test_empty_and_unknown_ids(self):
        self.record()
        self.inventory.return_value = []
        self.sync.handle(self.event('/查券'), self.send); self.wait()
        self.assertIn('未观测到任何券记录', self.sent[-1][1])
        self.inventory.return_value = [{'chargeId': 30001, 'stock': 0, 'purchaseDate': '', 'validDate': ''}]
        self.sync.handle(self.event('/查券'), self.send); self.wait()
        self.assertIn('chargeId 30001', self.sent[-1][1])

    def test_failure_is_redacted_and_gate_released(self):
        self.record()
        self.inventory.side_effect = RuntimeError('token-secret')
        self.sync.handle(self.event('/查券'), self.send); self.wait()
        self.assertIn('未做任何修改', self.sent[-1][1])
        self.assertNotIn('token-secret', repr(self.sent))

    def test_busy_gate_does_not_queue(self):
        self.record()
        self.assertTrue(self.sync.gate.acquire(blocking=False))
        self.addCleanup(self.sync.gate.release)
        self.sync.handle(self.event('/查券'), self.send)
        self.assertIn('正在进行', self.sent[-1][1]); self.inventory.assert_not_called()

    def test_cli_runner_args_and_failure(self):
        self.record()
        payload = json.dumps({'status': 'ok', 'tickets': self.rows}).encode()
        def run(argv, **kwargs):
            self.assertIn('--ticket-inventory', argv)
            self.assertEqual(argv[argv.index('--session-owner') + 1], USER)
            self.assertNotIn('--logout', argv); self.assertFalse(kwargs['shell'])
            return Mock(returncode=0, stdout=payload)
        with patch.object(q.subprocess, 'run', side_effect=run):
            self.assertEqual(q.inventory(USER, private=self.private), self.rows)
        with patch.object(q.subprocess, 'run', return_value=Mock(returncode=3, stdout=b'')):
            with self.assertRaisesRegex(q.CaptureError, 'inventory_unavailable'):
                q.inventory(USER, private=self.private)
        with self.assertRaisesRegex(q.CaptureError, 'inventory_unavailable'):
            q.inventory('99999999', private=self.private)

    def test_group_route_goes_private(self):
        self.record()
        with patch.object(q, 'DEFAULT_SYNC', self.sync), patch.object(main, '_group_qr_hint') as hint, \
             patch.object(main, 'ALLOWED_GROUP_IDS', frozenset({135990003})):
            main.handle(self.event('/查券') | {'message_type': 'group'})
            self.wait()
        self.inventory.assert_called_once_with(USER, private=self.private)
        # The answer is relayed by the group hint helper, never as a raw echo.
        self.assertTrue(hint.called)
        self.assertIn('2 倍券', repr(hint.call_args))


if __name__ == '__main__': unittest.main()
