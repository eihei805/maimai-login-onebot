"""Offline experimental typed upload tests; synthetic transport only."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from basic_settlement import SCHEMA, check
from item_upload import KINDS
import test_basic_settlement
import typed_settlement as typed


class Client:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []
        self.packet = None
        self.timeout = False
        self.mismatch = None
        self.music_pages = None
        self.bad_item_cursor = False

    def _current(self, session):
        pass

    def _game(self, api, body, identity):
        self.calls.append((api, copy.deepcopy(body)))
        if api == 'UpsertUserAllApi':
            self.packet = copy.deepcopy(body)
            if self.timeout:
                raise TimeoutError('SECRET')
            return dict(userId=1, returnCode=1)
        ua = self.packet['upsertUserAll']
        if api == 'GetUserMusicApi':
            if self.music_pages is not None:
                return self.music_pages[body['nextIndex']]
            rows = copy.deepcopy(ua['userMusicDetailList'])
            if rows and self.mismatch in SCHEMA['UserMusicDetail']:
                rows[0][self.mismatch] -= 1
            for r in rows:
                r['extNum2'] = 0
            return dict(userId=1, nextIndex=0, userMusicList=[dict(userMusicDetailList=rows)])
        if api == 'GetUserChargeApi':
            rows = copy.deepcopy(ua['userChargeList'])
            if self.mismatch == 'charges':
                rows[0]['stock'] -= 1
            for r in rows:
                r['extNum1'] = 0
            return dict(userId=1, userChargeList=rows, length=len(rows))
        if api == 'GetUserItemApi':
            kind = body['nextIndex'] // 10_000_000_000
            items = {(r['itemKind'], r['itemId']): copy.deepcopy(r) for r in self.snapshot['nodes']['userItemList']}
            items.update({(r['itemKind'], r['itemId']): copy.deepcopy(r) for r in ua['userItemList']})
            rows = [r for r in items.values() if r['itemKind'] == kind]
            if self.mismatch == 'items' and rows:
                rows[0]['stock'] += 1
            return dict(userId=1, itemKind=kind, userItemList=rows,
                        nextIndex=kind * 10_000_000_000 if self.bad_item_cursor else 0)
        raise AssertionError(api)


class TypedTests(unittest.TestCase):
    def setUp(self):
        fixture = test_basic_settlement.BuilderTests()
        fixture.setUp()
        self.snap, self.machine = fixture.snap, fixture.machine
        # Remove song-8 and use unrelated SD/DX charts with four/five levels.
        self.catalog = [next(r for r in fixture.kw['catalog'] if str(r['id']) == '8'),
                        dict(id='42', title='offline SD', type='SD', charts=[{} for _ in range(4)]),
                        dict(id='10042', title='offline DX', type='DX', charts=[{} for _ in range(5)])]
        self.snap['rawFavoriteKinds'] = [1, 2, 3, 10, 11]
        self.snap['itemKinds'] = [1, 2, 3, 10, 11]
        self.snap['nodes']['userItemList'] = [dict(itemKind=1, itemId=99, stock=3, isValid=True),
                                             dict(itemKind=10, itemId=7, stock=1, isValid=True)]
        self.snap['nodes']['userChargeList'] = [dict(chargeId=7, stock=4, purchaseDate='offline', validDate='offline')]
        self.op = dict(operation='score', music_id=10042, level=4, achievement=1010000, title='offline DX')
        self.session = SimpleNamespace(user_id=1, login_id=2, login_date_time=1800000000, user_agent_identity='offline')
        self.client = Client(self.snap)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = lambda: 1800000040

    def build(self):
        return typed.build_typed_settlement(self.snap, self.machine, user_id=1, login_id=2,
            login_date_time=1800000000, auth_date_time=1800000000, catalog=self.catalog,
            operation=self.op, now_unix=self.clock())

    def call(self, private=None):
        return typed.submit_typed(self.client, self.session, owner='offline-owner',
            private=private or self.tmp.name, snapshot=self.snap, machine=self.machine,
            auth_date_time=1800000000, catalog=self.catalog, operation=self.op, now=self.clock)

    def writes(self):
        return sum(api == 'UpsertUserAllApi' for api, _ in self.client.calls)

    def test_full_schema_scalar_without_plays_rating_or_counter_changes(self):
        before = copy.deepcopy((self.snap, self.machine, self.op))
        # Legacy grade data must not trigger the BASIC fixture rating guard.
        self.snap['nodes']['userData'][0]['gradeRating'] = 7
        packet = self.build()
        ua = packet['upsertUserAll']
        check(ua, 'UserAll', 'ua')
        self.assertEqual(packet['userPlaylogList'], [])
        self.assertEqual(ua['userGamePlaylogList'], [])
        self.assertEqual(ua['userMusicDetailList'], [dict(musicId=10042, level=4,
            playCount=0, achievement=1010000, scoreRank=13, comboStatus=0,
            syncStatus=0, deluxscoreMax=0, extNum1=0)])
        for name in ('userData', 'userExtend', 'userRatingList', 'userOption', 'userChargeList'):
            self.assertEqual(ua[name], self.snap['nodes'][name])
        self.snap['nodes']['userData'][0]['gradeRating'] = 0
        self.assertEqual((self.snap, self.machine, self.op), before)

    def test_old_detail_only_achievement_rank_change(self):
        old = dict(musicId=10042, level=4, playCount=9, achievement=1000000,
                   scoreRank=12, comboStatus=4, syncStatus=3, deluxscoreMax=900, extNum1=20)
        self.snap['nodes']['userMusicDetailList'] = [old]
        ua = self.build()['upsertUserAll']
        self.assertEqual(ua['userMusicDetailList'], [dict(old, achievement=1010000, scoreRank=13)])
        self.assertEqual(ua['isNewMusicDetailList'], '0')
        for score in (1000000, 999999):
            self.op['achievement'] = score
            self.assertEqual(self.call(), dict(status='rejected', reason='score_not_improved'))
        self.assertEqual(self.writes(), 0)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_all_native_rank_boundaries(self):
        for rank, threshold in enumerate(typed.RANK_THRESHOLDS, 1):
            for score, expected in ((threshold - 1, rank - 1), (threshold, rank)):
                self.op['achievement'] = score
                self.assertEqual(self.build()['upsertUserAll']['userMusicDetailList'][0]['scoreRank'], expected)

    def test_roundtrip_shared_claim_and_exact_readbacks(self):
        self.assertEqual(self.call(), dict(status='verified', checks=dict(charts=True, charges=True, items=True)))
        self.assertEqual(self.call()['reason'], 'already_claimed')
        self.assertEqual(self.writes(), 1)
        from settlement_submit import _claimed
        self.assertTrue(_claimed(self.tmp.name, (1, 2), 'offline-owner'))

    def test_arbitrary_cosmetic_kinds_id_and_preserved_inventory(self):
        for kind in list(KINDS) + list(KINDS.values()):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as private:
                self.op = dict(operation='item', kind=kind, item_id=123456)
                self.client = Client(self.snap)
                self.assertEqual(self.call(private), dict(status='verified', checks=dict(charts=True, charges=True, items=True)))
                ua = self.client.packet['upsertUserAll']
                self.assertEqual(ua['userMusicDetailList'][0]['musicId'], 8)
                self.assertEqual(ua['isNewItemList'], '1')
                self.assertEqual(ua['userItemList'][0]['itemId'], 123456)
                self.assertEqual(ua['userGamePlaylogList'][0]['useTicketId'], -1)
                self.assertEqual(ua['userData'][0]['playCount'], self.snap['nodes']['userData'][0]['playCount'] + 1)

    def test_already_owned_no_claim_no_write_and_zero_stock_add(self):
        self.op = dict(operation='item', kind=10, item_id=7)
        self.assertEqual(self.call(), dict(status='unchanged'))
        self.assertEqual(self.writes(), 0)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])
        self.snap['nodes']['userItemList'][1]['stock'] = 0
        ua = self.build()['upsertUserAll']
        self.assertEqual(ua['isNewItemList'], '0')
        self.assertEqual(ua['userItemList'][0]['stock'], 1)

    def test_invalid_inputs_no_claim_or_write(self):
        original = copy.deepcopy((self.snap, self.op, self.machine))
        mutations = [lambda: self.op.update(achievement=True), lambda: self.op.update(achievement=1010001),
            lambda: self.op.update(level=5), lambda: self.op.update(music_id=42),
            lambda: self.op.update(title='wrong'), lambda: self.snap.update(complete=False),
            lambda: self.snap.update(capturedAt=1799999999), lambda: self.snap.update(userId=2),
            lambda: self.snap['nodes']['userData'][0].pop('point'),
            lambda: self.snap['nodes']['userItemList'].append(copy.deepcopy(self.snap['nodes']['userItemList'][0])),
            lambda: self.snap['nodes']['userChargeList'].append(copy.deepcopy(self.snap['nodes']['userChargeList'][0])),
            lambda: self.machine.update(isFreePlay=0), lambda: self.machine.update(utcOffsetSeconds=60000),
            lambda: self.op.update(operation='item', kind=4, item_id=1),
            lambda: self.op.update(operation='item', kind=1, item_id=True)]
        for mutate in mutations:
            self.snap, self.op, self.machine = copy.deepcopy(original)
            mutate()
            self.assertEqual(self.call(), dict(status='rejected', reason='invalid_or_stale_input'))
        self.assertEqual(self.writes(), 0)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_item_dynamic_scope_reject_and_invalid_owned_reject(self):
        self.op = dict(operation='item', kind=3, item_id=12)
        self.snap['itemKinds'] = [1, 10]
        self.assertEqual(self.call()['reason'], 'invalid_or_stale_input')
        self.op = dict(operation='item', kind=10, item_id=7)
        self.snap['nodes']['userItemList'][1]['isValid'] = False
        self.assertEqual(self.call()['reason'], 'invalid_or_stale_input')

    def test_readback_every_chart_field_inventory_charges_and_timeout(self):
        for field in list(SCHEMA['UserMusicDetail']) + ['items', 'charges']:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as private:
                self.client = Client(self.snap)
                self.client.mismatch = field
                result = self.call(private)
                self.assertEqual(result['status'], 'acknowledged_unverified')
                self.assertFalse(result['checks']['charts' if field in SCHEMA['UserMusicDetail'] else field])
        self.client = Client(self.snap)
        self.client.timeout = True
        self.assertEqual(self.call()['status'], 'ambiguous')
        self.assertEqual(self.call()['reason'], 'already_claimed')
        self.assertEqual(self.writes(), 1)

    def test_item_cursor_cycle_and_music_missing_fail_readback(self):
        self.client.bad_item_cursor = True
        self.client.music_pages = {0: dict(userId=1, nextIndex=0, userMusicList=[])}
        result = self.call()
        self.assertEqual(result['status'], 'acknowledged_unverified')
        self.assertFalse(result['checks']['charts'])
        self.assertFalse(result['checks']['items'])
        self.assertEqual(self.writes(), 1)

    def test_freshness_and_binding_rechecked_after_claim(self):
        self.clock = iter((1800000040, 1800000400)).__next__
        self.assertEqual(self.call()['reason'], 'pre_send_validation_failed')
        self.assertEqual(self.writes(), 0)
        self.clock = lambda: 1800000040
        self.assertEqual(self.call()['reason'], 'already_claimed')

    def test_session_changes_after_claim_and_claim_failure(self):
        claim = typed._claim
        def changed(*args):
            result = claim(*args)
            self.session.login_id = 3
            return result
        with patch.object(typed, '_claim', side_effect=changed):
            self.assertEqual(self.call()['reason'], 'pre_send_validation_failed')
        self.assertEqual(self.writes(), 0)
        self.session.login_id = 2
        with patch.object(typed, '_claim', side_effect=OSError('SECRET')):
            self.assertEqual(self.call(), dict(status='rejected', reason='claim_failed'))

    def test_nonfinite_clock_and_offset_wall_session(self):
        self.clock = lambda: float('nan')
        self.assertEqual(self.call()['reason'], 'invalid_or_stale_input')
        self.clock = lambda: 1800000040
        self.machine['utcOffsetSeconds'] = 28800
        self.session.login_date_time += 28800
        self.snap['loginDateTime'] += 28800
        self.assertEqual(self.call()['status'], 'verified')


if __name__ == '__main__':
    unittest.main()
