"""Full private event path; synthetic capture, catalogs, transport, and sender."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import main
import qr_sync as q
import site_bindings as b
import score_upload as u
from test_score_upload import FISH, LXNS, ROW, Fake, lx

USER = '12345678'
QR = 'SGWCMAID' + 'X' * 12 + 'A' * 64


class PrivateIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = self.root / 'private'
        self.private.mkdir(mode=0o700)
        self.data = self.root / 'data'
        self.data.mkdir()
        (self.data / 'music_data.json').write_text(json.dumps(FISH))
        (self.data / 'songs.json').write_text(json.dumps(LXNS))
        self.bindings = b.SiteBindings(self.private)
        self.sent = []
        self.result = q.CaptureResult(USER, self.private / 'synthetic.json', [dict(ROW)],
                                      {'status': 'complete', 'rows': 1, 'logout': 'confirmed'})
        self.started, self.release = threading.Event(), threading.Event()
        self.release.set()
        def capture(user, qr, **kwargs):
            self.started.set()
            if not self.release.wait(2):
                raise RuntimeError('synthetic capture timed out')
            return self.result
        self.sync = q.QRSync(self.private, runner=capture, cooldown=0)
        self.transport = Fake([])
        for replacement in (
            patch('socket.socket', side_effect=AssertionError('offline')),
            patch.object(main, 'DATA', self.data),
            patch.object(main, 'send_private', side_effect=lambda uid, text: self.sent.append((uid, text))),
            patch.object(main, 'send_group', side_effect=AssertionError('no group sends')),
            patch.object(main, 'http_json', side_effect=AssertionError('no catalog refresh')),
            patch.object(u, 'https_transport', self.transport),
            patch.object(q, 'DEFAULT_SYNC', self.sync),
            patch.object(b, 'DEFAULT_BINDINGS', self.bindings),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        main.configure_private_sync()

    def event(self, text, user=USER, typ='private'):
        main.dispatch_frame(json.dumps(dict(post_type='message', message_type=typ,
            user_id=int(user), group_id=123456789, raw_message=text,
            message=[{'type': 'text', 'data': {'text': text}}])))

    def wait(self):
        self.assertTrue(self.sync.gate.acquire(timeout=3))
        self.sync.gate.release()

    def begin(self):
        self.event('/绑定水鱼 synthetic-original-fish')
        self.event('/绑定落雪 synthetic-original-personal')
        self.event('/上传状态')
        self.event('/同意HTTP取分')

    def both_answers(self):
        self.transport.answers = [(200, {'records': []}), (200, {'updates': 0, 'creates': 1}),
                                  lx({'friend_code': 123}), lx([]), lx(None)]

    def test_full_private_flow(self):
        self.begin()
        self.both_answers()
        self.event('/取分 ' + QR)
        self.wait()
        self.assertEqual(len(self.transport.calls), 5)
        self.assertIn('水鱼：已接受，待核验，落雪：已接受，待核验', self.sent[-1][1])
        self.assertTrue(all(uid == int(USER) for uid, text in self.sent))
        self.assertNotIn(QR, repr(self.sent))
        self.assertNotIn('synthetic-original', repr(self.sent))
        self.event('/解绑水鱼')
        self.event('/解绑落雪')
        self.assertEqual(self.bindings.credentials(USER), {})

    def test_login_upload_then_separate_logout(self):
        self.begin()
        self.both_answers()
        calls = []
        def retained(user, qr, **kwargs):
            self.assertTrue(kwargs['retain'])
            calls.append(('login', user))
            path = q.session_file(user, self.private)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as file:
                json.dump({'owner': user, 'version': 1, 'state': 'active'}, file)
            self.result.summary.update(logout='unconfirmed', session='active')
            return self.result
        def close(user, **kwargs):
            calls.append(('logout', user))
            q.session_file(user, self.private).unlink()
            return True
        self.sync.runner = retained
        self.sync.logout_runner = close
        self.event('/login ' + QR)
        self.wait()
        self.assertTrue(q.session_exists(USER, self.private))
        self.assertEqual(len(self.transport.calls), 5)
        self.assertTrue(any('会话已保留' in text for _, text in self.sent))
        self.event('/login ' + QR)
        self.wait()
        self.assertEqual(calls, [('login', USER)])
        self.event('/logout')
        self.wait()
        self.assertEqual(calls, [('login', USER), ('logout', USER)])
        self.assertFalse(q.session_exists(USER, self.private))
        self.assertIn('登出接口已接受', self.sent[-1][1])
        self.assertNotIn(QR, repr(self.sent))
        self.assertNotIn('synthetic-original', repr(self.sent))

    def test_rebind_during_capture_uses_original_owner_snapshot(self):
        self.begin()
        self.both_answers()
        self.release.clear()
        self.event(QR)
        self.assertTrue(self.started.wait(1))
        try:
            self.event('/绑定水鱼 synthetic-new-fish')
            self.event('/解绑落雪')
            self.event('/绑定落雪 synthetic-other-owner', user='98765432')
        finally:
            self.release.set()
        self.wait()
        self.assertEqual(self.transport.calls[0][2], {'Import-Token': 'synthetic-original-fish'})
        self.assertEqual(self.transport.calls[2][2], {'X-User-Token': 'synthetic-original-personal'})
        self.assertEqual(self.bindings.credentials(USER), {'fish_import_token': 'synthetic-new-fish'})

    def test_incomplete_and_groups_never_upload(self):
        self.begin()
        self.result.summary['status'] = 'partial'
        self.event(QR)
        self.wait()
        self.assertEqual(self.transport.calls, [])
        before = list(self.sent)
        for text in (QR, '/绑定水鱼 secret', '/解绑落雪', '/上传状态', '/取分 ' + QR):
            self.event(text, typ='group')
        self.assertEqual(self.sent, before)

    def test_missing_catalog_blocks_and_redacts(self):
        self.begin()
        (self.data / 'songs.json').unlink()
        self.event(QR)
        self.wait()
        self.assertEqual(self.transport.calls, [])
        self.assertIn('上传未完成', self.sent[-1][1])
        self.assertNotIn(str(self.root), repr(self.sent))

    def test_missing_snapshot_and_wrong_owner_fail_closed(self):
        self.begin()
        self.bindings.upload_callback(self.result, lambda uid, text: self.sent.append((uid, text)))
        self.assertIn('快照', self.sent[-1][1])
        self.result.upload_context = self.bindings.snapshot('98765432')
        self.bindings.upload_callback(self.result, lambda uid, text: self.sent.append((uid, text)))
        self.assertIn('快照', self.sent[-1][1])
        self.assertEqual(self.transport.calls, [])

    def test_unbound_before_capture_stays_unbound(self):
        self.event('/同意HTTP取分')
        self.release.clear()
        self.event(QR)
        self.assertTrue(self.started.wait(1))
        try:
            self.event('/绑定水鱼 synthetic-new-fish')
        finally:
            self.release.set()
        self.wait()
        self.assertEqual(self.transport.calls, [])
        self.assertIn('未配置上传凭据', self.sent[-1][1])

    def test_capture_owner_mismatch_blocks_callback(self):
        self.begin()
        self.result.requester = '98765432'
        self.event(QR)
        self.wait()
        self.assertEqual(self.transport.calls, [])
        self.assertIn('取分失败', self.sent[-1][1])
        self.assertTrue(all(uid == int(USER) for uid, text in self.sent))

    def test_snapshot_failure_never_starts_capture_and_releases_gate(self):
        self.begin()
        with patch.object(self.bindings, 'snapshot', side_effect=RuntimeError('secret-token')):
            self.event(QR)
        self.wait()
        self.assertFalse(self.started.is_set())
        self.assertEqual(self.transport.calls, [])
        self.assertIn('暂不可用', self.sent[-1][1])
        self.assertNotIn('secret-token', repr(self.sent))


if __name__ == '__main__':
    unittest.main()
