"""CLI orchestration with an offline collector and submitter; no real account."""
import contextlib
import io
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch
from test_ticket_cli import TicketCLITests, live, USER

class SettlementCLITests(unittest.TestCase):
    def setUp(self):
        TicketCLITests.setUp(self)
        self.plan_path=self.private/'settlement-plan-test.json'
        self.plan=dict(owner=USER,machine={'utcOffsetSeconds':0},extend_flags={'isPhotoAgree':False,'isGotoCodeRead':False},auth_date_time=1000,kind=1,item_ids=[201])

    unchanged = TicketCLITests.unchanged

    def settle(self, status='verified'):
        self.plan_path.write_text(json.dumps(self.plan));self.plan_path.chmod(0o600)
        collector=Mock(return_value={'synthetic':True})
        submit=Mock(return_value={'status':status})
        module=types.ModuleType('settlement_submit');module.submit_basic=submit
        output=io.StringIO()
        with patch.object(live,'load_config',return_value=(self.config,{})), patch('settlement_collect.collect_snapshot',collector), patch.dict(sys.modules,{'settlement_submit':module}), patch.object(sys,'argv',['client','--settle-basic','--session-owner',USER,'--settlement-plan',str(self.plan_path)]),contextlib.redirect_stdout(output):
            code=live.main()
        return code,json.loads(output.getvalue()),collector,submit

    def test_cli_restores_collects_and_submits_without_refreshing_session(self):
        code,out,collector,submit=self.settle()
        self.assertEqual(code,0);self.assertEqual(out,{'status':'verified'})
        collector.assert_called_once();submit.assert_called_once()
        kw=submit.call_args.kwargs
        self.assertEqual(kw['owner'],USER);self.assertEqual(kw['build_args']['item_ids'],[201])
        self.assertEqual(kw['build_args']['user_id'],123)
        self.unchanged()

    def test_cli_unconfirmed_is_not_success(self):
        code,out,_,_=self.settle('ambiguous')
        self.assertEqual(code,3);self.assertEqual(out['status'],'ambiguous');self.unchanged()

    def test_other_target_fails_before_collection(self):
        self.plan['item_ids']=[202]
        with self.assertRaisesRegex(live.SafeFailure,'target_rejected'):self.settle()
        self.unchanged()

    def test_owner_mismatch_rejected(self):
        self.plan['owner']='87654321'
        with self.assertRaisesRegex(live.SafeFailure,'plan_rejected'):self.settle()

if __name__=='__main__':unittest.main()
