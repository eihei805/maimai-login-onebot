"""Scalar upload routing offline only; no writes."""
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest
import main
import qr_sync as q

class TypedCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name); self.private.chmod(0o700)
        self.sync = q.QRSync(self.private)
        self.sent = []
    def send(self, user, text): self.sent.append(text)
    def event(self, text):
        return dict(post_type='message', message_type='private', user_id=12345678,raw_message=text)
    def test_item_missing_session_is_explicit(self):
        with patch.object(q, 'typed_upload') as upload:
            self.assertTrue(self.sync.handle(self.event('/上传物品 1 201'), self.send))
            self.assertTrue(self.sync.gate.acquire(timeout=2)); self.sync.gate.release()
            upload.assert_not_called()
        self.assertIn('/login', self.sent[-1])
    def test_malformed_not_silent(self):
        for text in ('/上传物品', '/上传成绩 8 绿 xx'):
            self.assertTrue(self.sync.handle(self.event(text), self.send))
        self.assertEqual(len(self.sent), 2)
    def test_group_and_private_routes(self):
        for typ in ('private', 'group'):
            for text in ('/上传物品 1 201', '/上传成绩 8 绿 1010000'):
                event = dict(self.event(text), message_type=typ, group_id=123456789)
                with patch.object(main, 'ALLOWED_GROUP_IDS', frozenset({123456789})), \
                     patch('site_bindings.handle', return_value=False), \
                     patch.object(q.DEFAULT_SYNC, 'handle') as handle:
                    main.handle(event)
                    handle.assert_called_once()
                    self.assertEqual(handle.call_args.args[0]['message_type'], 'private')
                    self.assertEqual(handle.call_args.args[0]['raw_message'], text)

    def test_valid_score_missing_session_does_not_write(self):
        with patch.object(q, 'typed_upload') as upload:
            self.assertTrue(self.sync.handle(self.event('/上传成绩 8 绿 1010000'),self.send))
            self.assertTrue(self.sync.gate.acquire(timeout=2));self.sync.gate.release()
            upload.assert_not_called()
        self.assertIn('/login',self.sent[-1])
