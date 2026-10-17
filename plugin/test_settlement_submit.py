"""Entirely offline coordinator tests; no client runtime imports."""
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import settlement_submit as submit
import test_basic_settlement


class FakeClient:
    def __init__(self):
        self.calls = []
        self.packet = None
        self.timeout = False
        self.mismatch = None
        self.read_failure = False
        self.code = 1

    def _current(self, session):
        pass

    def _game(self, api, body, identity):
        self.calls.append((api, copy.deepcopy(body)))
        if api == 'UpsertUserAllApi':
            self.packet = copy.deepcopy(body)
            if self.timeout:
                raise TimeoutError('secret must not escape')
            return dict(returnCode=self.code)
        if self.read_failure:
            raise RuntimeError('secret response')
        ua = self.packet['upsertUserAll']
        if api == 'GetUserItemApi':
            return dict(userId=1, nextIndex=0, userItemList=[] if self.mismatch == 'items' else ua['userItemList'])
        if api == 'GetUserDataApi':
            assert body == {'userId': 1}
            profile = copy.deepcopy(ua['userData'][0])
            if self.mismatch in ('playCount', 'currentPlayCount'):
                profile[self.mismatch] -= 1
            if self.mismatch == 'lastPlayDate':
                profile['lastPlayDate'] = 'old'
            if self.mismatch == 'partial_profile':
                profile = {k: profile[k] for k in ('playCount', 'currentPlayCount', 'lastPlayDate')}
            return dict(userId=1, banState=0, userData=profile)
        if api == 'GetUserMusicApi':
            rows = copy.deepcopy(ua['userMusicDetailList'])
            if self.mismatch == 'chart':
                rows[0]['playCount'] -= 1
            return dict(userId=1, nextIndex=0, userMusicList=[dict(userMusicDetailList=rows)])
        raise AssertionError(api)


class SubmitTests(unittest.TestCase):
    def setUp(self):
        fixture = test_basic_settlement.BuilderTests()
        fixture.setUp()
        self.snapshot, self.machine, self.args = fixture.snap, fixture.machine, fixture.kw
        self.session = SimpleNamespace(user_id=1, login_id=2, login_date_time=1800000000,
                                       user_agent_identity='offline')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = FakeClient()
        self.clock = lambda: 1800000040

    def call(self):
        return submit.submit_basic(self.client, self.session, owner='offline-owner',
            private=self.tmp.name, snapshot=self.snapshot, machine=self.machine,
            build_args=self.args, now=self.clock)

    def writes(self):
        return sum(api == 'UpsertUserAllApi' for api, _ in self.client.calls)

    def test_verified_duplicate_and_durable_private_marker(self):
        self.assertEqual(self.call(), dict(status='verified', checks=dict(items=True, profile=True, chart=True)))
        self.assertEqual(self.call(), dict(status='rejected', reason='already_claimed'))
        self.assertEqual(self.writes(), 1)
        marker, = Path(self.tmp.name).iterdir()
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('offline-owner', marker.read_text())
        self.client = FakeClient()  # simulates another process / client instance
        self.assertEqual(self.call()['reason'], 'already_claimed')
        self.assertEqual(self.writes(), 0)

    def test_timeout_no_retry_even_when_readback_matches(self):
        self.client.timeout = True
        out = self.call()
        self.assertEqual(out['status'], 'ambiguous')
        self.assertNotIn('secret', str(out))
        self.call()
        self.assertEqual(self.writes(), 1)

    def test_read_failure_and_mismatch(self):
        for mismatch in ('items', 'chart', 'playCount', 'currentPlayCount', 'lastPlayDate', 'partial_profile', 'failure'):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as directory:
                original = self.tmp.name
                self.tmp.name = directory
                try:
                    self.client = FakeClient()
                    self.client.mismatch = mismatch
                    self.client.read_failure = mismatch == 'failure'
                    self.assertEqual(self.call()['status'], 'acknowledged_unverified')
                    self.assertEqual(self.call()['reason'], 'already_claimed')
                    self.assertEqual(self.writes(), 1)
                finally:
                    self.tmp.name = original

    def test_stale_input_and_now_override(self):
        self.args['now_unix'] = 1
        self.assertEqual(self.call()['status'], 'verified')
        self.clock = lambda: 1800000400
        self.assertEqual(self.call()['status'], 'rejected')
        self.assertEqual(self.writes(), 1)

    def test_freshness_checked_after_claim(self):
        clock = iter((1800000040, 1800000040, 1800000400))
        self.clock = lambda: next(clock)
        self.assertEqual(self.call()['reason'], 'pre_send_validation_failed')
        self.assertEqual(self.writes(), 0)
        self.clock = lambda: 1800000040
        self.assertEqual(self.call()['reason'], 'already_claimed')

    def test_invalid_packets_never_claim_or_send(self):
        builder = submit.build_basic_settlement
        mutations = [lambda p: p['upsertUserAll'].__setitem__('isNewItemList', '9'),
                     lambda p: p['upsertUserAll']['userData'][0].__setitem__('playCount', True),
                     lambda p: p.__setitem__('userId', 2),
                     lambda p: p['userPlaylogList'][0].__setitem__('playlogId', 3)]
        for mutate in mutations:
            def bad(*args, **kw):
                packet = builder(*args, **kw)
                mutate(packet)
                return packet
            with patch.object(submit, 'build_basic_settlement', bad):
                self.assertEqual(self.call()['status'], 'rejected')
        self.assertEqual(self.writes(), 0)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_missing_machine_and_wrong_binding(self):
        del self.machine['placeId']
        self.assertEqual(self.call()['status'], 'rejected')
        self.assertEqual(self.writes(), 0)
        self.machine['placeId'] = 99
        self.session.user_id = 7
        self.assertEqual(self.call()['status'], 'rejected')

    def test_directory_permissions_and_symlink(self):
        os.chmod(self.tmp.name, 0o755)
        self.assertEqual(self.call()['reason'], 'claim_failed')
        os.chmod(self.tmp.name, 0o700)
        with tempfile.TemporaryDirectory() as parent:
            link = Path(parent) / 'link'
            link.symlink_to(self.tmp.name, target_is_directory=True)
            original = self.tmp.name
            self.tmp.name = str(link)
            try:
                self.assertEqual(self.call()['reason'], 'claim_failed')
            finally:
                self.tmp.name = original
        self.assertEqual(self.writes(), 0)

    def test_existing_symlink_claim_is_not_followed(self):
        name = hashlib.sha256(json.dumps(['offline-owner', 1, 2], separators=(',', ':')).encode()).hexdigest() + '.claim'
        target = Path(self.tmp.name) / 'untouched'
        target.write_text('original')
        (Path(self.tmp.name) / name).symlink_to(target)
        self.assertEqual(self.call()['reason'], 'already_claimed')
        self.assertEqual(target.read_text(), 'original')
        self.assertEqual(self.writes(), 0)

    def test_fsync_before_send_and_failure_consumes_claim(self):
        with patch.object(submit.os, 'fsync', side_effect=OSError('private path secret')):
            self.assertEqual(self.call()['reason'], 'claim_failed')
        self.assertEqual(self.call()['reason'], 'already_claimed')
        self.assertEqual(self.writes(), 0)

    def test_negative_or_malformed_ack_is_ambiguous(self):
        self.client.code = True
        self.assertEqual(self.call()['status'], 'ambiguous')
        self.assertEqual(self.writes(), 1)


if __name__ == '__main__':
    unittest.main()
