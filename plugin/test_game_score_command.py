"""Command routing tests: offline, never writes a game score."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import main
import qr_sync as q

class GameScoreCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name)
        self.private.chmod(0o700)
        self.sync = q.QRSync(self.private)
        self.event = dict(post_type='message', message_type='private', user_id=12345678,
                          raw_message='/上传游戏成绩')
        self.sent = []
    def send(self, uid, text): self.sent.append(text)
    def wait(self):
        self.assertTrue(self.sync.gate.acquire(timeout=2))
        self.sync.gate.release()
    def record(self):
        p = self.private / 'session-12345678.json'
        p.write_text(json.dumps(dict(state='active', owner='12345678')))
        p.chmod(0o600)
    def test_missing_session_does_not_upload(self):
        with patch.object(q, 'game_score_upload') as upload:
            self.assertTrue(self.sync.handle(self.event, self.send))
            self.wait()
            upload.assert_not_called()
            self.assertIn('本次未上传', self.sent[-1])
    def test_missing_native_data_explained(self):
        self.record()
        with patch.object(q, 'game_score_upload') as upload:
            self.assertTrue(self.sync.handle(self.event, self.send))
            self.wait()
            upload.assert_not_called()
            self.assertIn('原生游玩结算数据', self.sent[-1])
    def test_private_and_group_reach_same_handler(self):
        for typ in ('private', 'group'):
            event = dict(self.event, message_type=typ, group_id=123456789)
            with patch.object(main, 'ALLOWED_GROUP_IDS', frozenset({123456789})), \
                 patch('site_bindings.handle', return_value=False), \
                 patch.object(q.DEFAULT_SYNC, 'handle') as handle:
                main.handle(event)
                handle.assert_called_once()
                self.assertEqual(handle.call_args.args[0]['message_type'], 'private')
                self.assertEqual(handle.call_args.args[0]['user_id'], 12345678)

    def test_cli_uses_fixed_owner_document_and_no_qr(self):
        p = self.private / 'game-score-12345678.json'
        p.write_text('{}')
        p.chmod(0o600)
        response = Mock(stdout=b'{"status":"verified"}', returncode=0)
        with patch.object(q.subprocess, 'run', return_value=response) as run:
            out = q.game_score_upload('12345678', private=self.private)
        self.assertEqual(out['status'], 'verified')
        argv = run.call_args.args[0]
        self.assertIn('--upload-game-scores', argv)
        self.assertEqual(argv[-1], str(p))
        self.assertNotIn('--live', argv)
        self.assertNotIn('--ticket-purchase', argv)
