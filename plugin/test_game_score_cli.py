"""Offline CLI wiring: no actual transport is called."""
import contextlib
import io
import json
import sys
import unittest
from unittest.mock import Mock, patch
from test_ticket_cli import TicketCLITests, live, USER

class GameScoreCLITests(unittest.TestCase):
    def setUp(self):
        TicketCLITests.setUp(self)
        self.score_path = self.private / ('game-score-' + USER + '.json')
        self.doc = dict(version=1, owner=USER, extend_flags=dict(isPhotoAgree=False, isGotoCodeRead=False))
    unchanged = TicketCLITests.unchanged
    def run_import(self, status='verified'):
        self.score_path.write_text(json.dumps(self.doc)); self.score_path.chmod(0o600)
        collect = Mock(return_value={'fresh':'snapshot'})
        submit = Mock(return_value={'status':status})
        out = io.StringIO()
        with patch.object(live, 'load_config', return_value=(self.config, {})), \
             patch('settlement_collect.collect_snapshot', collect), \
             patch('game_score_upload.submit_scores', submit), \
             patch.object(sys, 'argv', ['client','--upload-game-scores','--session-owner',USER,'--score-document',str(self.score_path)]), \
             contextlib.redirect_stdout(out):
            code = live.main()
        return code, json.loads(out.getvalue()), collect, submit
    def test_wires_fresh_snapshot_and_current_session(self):
        code, out, collect, submit = self.run_import()
        self.assertEqual(code, 0)
        collect.assert_called_once()
        submit.assert_called_once()
        self.assertEqual(submit.call_args.kwargs['snapshot'], {'fresh':'snapshot'})
        self.assertEqual(submit.call_args.kwargs['document'], self.doc)
        self.assertEqual(submit.call_args.kwargs['owner'], USER)
        self.unchanged()
    def test_ambiguous_does_not_report_success(self):
        self.assertEqual(self.run_import('ambiguous')[0], 3)
        self.unchanged()
    def test_document_cannot_belong_to_other_owner(self):
        self.doc['owner'] = '87654321'
        with self.assertRaisesRegex(live.SafeFailure, 'document_rejected'):
            self.run_import()
        self.unchanged()
