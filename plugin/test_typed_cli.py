"""Typed CLI orchestration with mocked collector/coordinator."""
import contextlib
import io
import json
import sys
import unittest
from unittest.mock import Mock, patch
from test_ticket_cli import TicketCLITests, live, USER

class TypedCLITests(unittest.TestCase):
    def setUp(self):
        TicketCLITests.setUp(self)
        self.operation = dict(operation='item', kind=1, item_id=201)
        profile = dict(machine=dict(utcOffsetSeconds=28800), extend_flags=dict(isPhotoAgree=False,isGotoCodeRead=False))
        p=self.private/'ticket-machine-profile.json';p.write_text(json.dumps(profile));p.chmod(0o600)
    unchanged = TicketCLITests.unchanged
    def run_typed(self, status='verified'):
        collect=Mock(return_value={'fresh':True}); submit=Mock(return_value={'status':status})
        out=io.StringIO()
        with patch.object(live,'load_config',return_value=(self.config,{})), \
             patch('settlement_collect.collect_snapshot',collect), \
             patch('typed_settlement.submit_typed',submit), \
             patch.object(sys,'argv',['client','--upload-typed','--session-owner',USER,'--upload-operation',json.dumps(self.operation)]), \
             contextlib.redirect_stdout(out):
            code=live.main()
        return code,json.loads(out.getvalue()),collect,submit
    def test_collects_affected_item_kind_and_passes_scalar_target(self):
        code,out,collect,submit=self.run_typed()
        self.assertEqual(code,0);self.assertEqual(collect.call_args.kwargs['item_kinds'],(1,))
        self.assertEqual(submit.call_args.kwargs['operation'],self.operation)
        self.unchanged()
    def test_indeterminate_exit_is_not_success(self):
        self.assertEqual(self.run_typed('ambiguous')[0],3);self.unchanged()
    def test_bad_target_before_transport(self):
        self.operation['kind']=999
        with self.assertRaises(ValueError): self.run_typed()
        self.unchanged()

    def test_profile_provenance_is_metadata_only(self):
        p = self.private / 'ticket-machine-profile.json'
        data = json.loads(p.read_text())
        data['provenance'] = {'source': 'explicit simulation'}
        p.write_text(json.dumps(data))
        self.assertEqual(self.run_typed()[0], 0)
        self.unchanged()
