"""Synthetic native packet tests; no live transport, credentials or runtime."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from basic_settlement import SCHEMA
import game_score_upload as upload
import test_basic_settlement


class NativeClient:
    def __init__(self):
        self.calls = []
        self.packet = None
        self.timeout = False
        self.mismatch = None
        self.pages = None

    def _current(self, session):
        pass

    def _game(self, api, body, identity):
        self.calls.append((api, copy.deepcopy(body)))
        if api == 'UpsertUserAllApi':
            self.packet = copy.deepcopy(body)
            if self.timeout:
                raise TimeoutError('SECRET transport details')
            return dict(userId=1, returnCode=1)
        ua = self.packet['upsertUserAll']
        if api == 'GetUserChargeApi':
            rows = copy.deepcopy(ua['userChargeList'])
            if self.mismatch == 'charges':
                rows[0]['stock'] -= 1
            for row in rows:
                row['extNum1'] = 0
            return dict(userId=1, length=len(rows), userChargeList=rows)
        if api == 'GetUserMusicApi':
            if self.pages is not None:
                return copy.deepcopy(self.pages[body['nextIndex']])
            rows = copy.deepcopy(ua['userMusicDetailList'])
            if self.mismatch in rows[0]:
                rows[0][self.mismatch] -= 1
            for row in rows:
                row['extNum2'] = 0
            return dict(userId=1, nextIndex=0, userMusicList=[dict(userMusicDetailList=rows)])
        raise AssertionError(api)


class NativeTests(unittest.TestCase):
    def setUp(self):
        fixture = test_basic_settlement.BuilderTests()
        fixture.setUp()
        fixture.snap['nodes']['userChargeList'] = [dict(chargeId=7, stock=3,
                                                       purchaseDate='native', validDate='native')]
        fixture.kw['item_ids'] = []
        packet = fixture.build()
        base = copy.deepcopy(packet['upsertUserAll'])
        for key, value in fixture.snap['nodes'].items():
            base[key] = copy.deepcopy(value)
        base['userGamePlaylogList'] = []
        for bits, array in upload.BITS_TO_ARRAY.items():
            if bits != 'isNewFavoriteList':
                base[bits] = '0' * len(base[array])
        self.snapshot = fixture.snap
        self.document = dict(version=1, owner='offline-owner', session=dict(userId=1,
            loginId=2, loginDateTime=1800000000, userAgentIdentity='offline', utcOffsetSeconds=0),
            baseline=base, request=packet)
        self.session = SimpleNamespace(user_id=1, login_id=2, login_date_time=1800000000,
                                       user_agent_identity='offline')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = NativeClient()
        self.clock = lambda: 1800000040

    def call(self):
        return upload.submit_native(self.client, self.session, owner='offline-owner',
            private=self.tmp.name, document=self.document, snapshot=self.snapshot, now=self.clock)

    def writes(self):
        return sum(api == 'UpsertUserAllApi' for api, _ in self.client.calls)

    def assert_rejected(self):
        self.assertEqual(self.call(), dict(status='rejected', reason='invalid_or_stale_input'))
        self.assertEqual(self.writes(), 0)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_all_miss_native_roundtrip_immutable_and_shared_claim(self):
        before = copy.deepcopy((self.document, self.snapshot))
        self.assertEqual(self.call(), dict(status='verified', checks=dict(charts=True, charges=True)))
        self.assertEqual((self.document, self.snapshot), before)
        self.assertEqual(self.client.packet, self.document['request'])
        self.assertEqual(self.call()['reason'], 'already_claimed')
        self.assertEqual(self.writes(), 1)
        from settlement_submit import _claimed
        self.assertTrue(_claimed(self.tmp.name, (1, 2), 'offline-owner'))

    def test_cli_submit_scores_accepts_collection_extend_flags(self):
        self.document['extend_flags'] = dict(isPhotoAgree=False, isGotoCodeRead=False)
        result = upload.submit_scores(self.client, self.session, owner='offline-owner',
            private=self.tmp.name, document=self.document, snapshot=self.snapshot, now=self.clock)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(self.client.packet, self.document['request'])

    def test_high_native_scores_and_rating_pass_through(self):
        ua = self.document['request']['upsertUserAll']
        log = self.document['request']['userPlaylogList'][0]
        for name in ('tap', 'hold', 'slide', 'touch', 'break'):
            log[name+'CriticalPerfect'] = log[name+'Miss']
            log[name+'Miss'] = 0
        log.update(achievement=1010000, deluxscore=288, maxCombo=96, comboStatus=4,
                   syncStatus=3, scoreRank=13, afterRating=1400, afterDeluxRating=1400)
        ua['userMusicDetailList'][0].update(achievement=1010000, deluxscoreMax=288,
            comboStatus=4, syncStatus=3, scoreRank=13)
        ua['userData'][0].update(musicRating=1400, playerRating=1400, highestRating=1400)
        ua['userRatingList'][0]['udemae']['classValue'] = 20000
        self.assertEqual(self.call()['status'], 'verified')
        self.assertEqual(self.client.packet, self.document['request'])

    def test_owner_session_and_native_packet_binding_mismatch(self):
        mutations = [lambda d: d.__setitem__('owner', 'other'),
            lambda d: d['session'].__setitem__('loginId', 3),
            lambda d: d['session'].__setitem__('userAgentIdentity', 'old'),
            lambda d: d['request'].__setitem__('loginDateTime', 1800000001),
            lambda d: d['request']['userPlaylogList'][0].__setitem__('loginDate', 1),
            lambda d: d['request']['userPlaylogList'][0].__setitem__('playlogId', 3)]
        original = copy.deepcopy(self.document)
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.document = copy.deepcopy(original)
                mutate(self.document)
                self.assert_rejected()

    def test_timeout_is_ambiguous_and_never_retried(self):
        self.client.timeout = True
        result = self.call()
        self.assertEqual(result, dict(status='ambiguous', checks=dict(charts=True, charges=True)))
        self.assertNotIn('SECRET', str(result))
        self.assertEqual(self.call()['reason'], 'already_claimed')
        self.assertEqual(self.writes(), 1)

    def test_charge_and_unrelated_mutations_rejected(self):
        original = copy.deepcopy(self.document)
        for key in ('userChargeList', 'userOption', 'userFavoriteList', 'userItemList'):
            self.document = copy.deepcopy(original)
            ua = self.document['request']['upsertUserAll']
            if key == 'userChargeList': ua[key][0]['stock'] -= 1
            elif key == 'userOption': ua[key][0]['dispJudge'] += 1
            elif key == 'userFavoriteList': ua[key][0]['itemIdList'] = []
            else: ua[key] = [dict(itemKind=1, itemId=11, stock=1, isValid=True)]
            with self.subTest(key=key): self.assert_rejected()
        self.document = copy.deepcopy(original)
        self.document['request']['upsertUserAll']['userData'][0]['iconId'] = 9
        self.assert_rejected()
        self.document = copy.deepcopy(original)
        self.document['request']['upsertUserAll']['userGamePlaylogList'][0]['useTicketId'] = 7
        self.assert_rejected()

    def test_every_readback_detail_field_and_charge_inventory_checked(self):
        for field in SCHEMA['UserMusicDetail']:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as private:
                self.client = NativeClient()
                self.client.mismatch = field
                result = upload.submit_native(self.client, self.session, owner='offline-owner',
                    private=private, document=self.document, snapshot=self.snapshot, now=self.clock)
                self.assertEqual(result['status'], 'acknowledged_unverified')
                self.assertFalse(result['checks']['charts'])
        self.client.mismatch = 'charges'
        self.assertEqual(self.call(), dict(status='acknowledged_unverified', checks=dict(charts=True, charges=False)))

    def test_music_pages_checked_to_end_duplicates_cycles_and_missing(self):
        target = copy.deepcopy(self.document['request']['upsertUserAll']['userMusicDetailList'][0])
        other = dict(target, musicId=9)
        def page(rows, nxt):
            return dict(userId=1, nextIndex=nxt, userMusicList=[dict(userMusicDetailList=rows)])
        variants = [({0: page([other], 50), 50: page([target], 0)}, True),
                    ({0: page([target], 50), 50: page([target], 0)}, False),
                    ({0: page([target], 50), 50: page([other], 50)}, False),
                    ({0: page([other], 0)}, False),
                    ({0: page([target], 50), 50: page([], 100)}, False)]
        for pages, expected in variants:
            with self.subTest(pages=pages), tempfile.TemporaryDirectory() as private:
                self.client = NativeClient()
                self.client.pages = pages
                result = upload.submit_native(self.client, self.session, owner='offline-owner',
                    private=private, document=self.document, snapshot=self.snapshot, now=self.clock)
                self.assertEqual(result['checks']['charts'], expected)
                self.assertEqual(self.writes(), 1)

    def test_readback_page_budget_is_finite(self):
        target = copy.deepcopy(self.document['request']['upsertUserAll']['userMusicDetailList'][0])
        self.client.pages = {i: dict(userId=1, nextIndex=i+1,
            userMusicList=[dict(userMusicDetailList=[dict(target, musicId=100+i)])])
            for i in range(128)}
        result = self.call()
        self.assertEqual(result['status'], 'acknowledged_unverified')
        self.assertFalse(result['checks']['charts'])
        self.assertEqual(sum(api == 'GetUserMusicApi' for api, _ in self.client.calls), 128)
        self.assertEqual(self.writes(), 1)

    def test_native_sync_special_five_merge_and_threshold_counter(self):
        original = copy.deepcopy(self.document)
        for previous, played, expected in ((0, 5, 5), (5, 0, 5), (5, 1, 1),
                                           (5, 4, 4), (1, 5, 1), (4, 2, 4)):
            with self.subTest(previous=previous, played=played), tempfile.TemporaryDirectory() as private:
                self.document = copy.deepcopy(original)
                ua = self.document['request']['upsertUserAll']
                detail = ua['userMusicDetailList'][0]
                old = dict(detail, playCount=3, syncStatus=previous)
                self.snapshot['nodes']['userMusicDetailList'] = [copy.deepcopy(old)]
                self.document['baseline']['userMusicDetailList'] = [copy.deepcopy(old)]
                self.document['baseline']['isNewMusicDetailList'] = '0'
                ua['isNewMusicDetailList'] = '0'
                detail.update(playCount=4, syncStatus=expected, extNum1=2)
                self.document['request']['userPlaylogList'][0]['syncStatus'] = played
                self.client = NativeClient()
                result = upload.submit_scores(self.client, self.session, owner='offline-owner',
                    private=private, document=self.document, snapshot=self.snapshot, now=self.clock)
                self.assertEqual(result['status'], 'verified')
                # A numeric max or an arbitrary aggregate must not be accepted.
                detail['syncStatus'] = 0 if expected else 1
                self.assertEqual(self.call()['reason'], 'invalid_or_stale_input')

    def test_old_best_never_decreases_and_playcount_exact(self):
        detail = self.document['request']['upsertUserAll']['userMusicDetailList'][0]
        old = dict(detail, playCount=5, achievement=1000000, deluxscoreMax=250,
                   comboStatus=3, syncStatus=2, scoreRank=12)
        self.snapshot['nodes']['userMusicDetailList'] = [copy.deepcopy(old)]
        self.document['baseline']['userMusicDetailList'] = [copy.deepcopy(old)]
        self.document['baseline']['isNewMusicDetailList'] = '0'
        self.document['request']['upsertUserAll']['isNewMusicDetailList'] = '0'
        detail.update(old, playCount=6)
        for field in upload.BEST + ('playCount',):
            value = detail[field]
            detail[field] -= 1
            with self.subTest(field=field): self.assert_rejected()
            detail[field] = value
        self.assertEqual(self.call()['status'], 'verified')

    def test_malformed_missing_duplicate_unbounded_and_stale_inputs(self):
        original = copy.deepcopy(self.document)
        mutations = [lambda d: d['request']['upsertUserAll']['userData'][0].pop('point'),
            lambda d: d['request']['userPlaylogList'].append(copy.deepcopy(d['request']['userPlaylogList'][0])),
            lambda d: d['request']['upsertUserAll']['userMusicDetailList'].append(copy.deepcopy(d['request']['upsertUserAll']['userMusicDetailList'][0])),
            lambda d: d.__setitem__('version', True),
            lambda d: d['request']['upsertUserAll']['userData'][0].__setitem__('playCount', True),
            lambda d: d['baseline']['userChargeList'].append(copy.deepcopy(d['baseline']['userChargeList'][0])),
            lambda d: d.__setitem__('extra', 'x'*8_000_001)]
        for mutate in mutations:
            self.document = copy.deepcopy(original)
            mutate(self.document)
            self.assert_rejected()
        self.document = original
        self.clock = lambda: 1800000400
        self.assert_rejected()
        self.clock = lambda: 1800000040
        self.snapshot['complete'] = False
        self.assert_rejected()

    def test_freshness_and_session_rechecked_after_durable_claim(self):
        self.clock = iter((1800000040, 1800000400)).__next__
        self.assertEqual(self.call()['reason'], 'pre_send_validation_failed')
        self.assertEqual(self.writes(), 0)
        self.clock = lambda: 1800000040
        self.assertEqual(self.call()['reason'], 'already_claimed')

    def test_session_change_after_claim_prevents_send(self):
        claim = upload._claim
        def change(*args):
            result = claim(*args)
            self.session.login_id = 3
            return result
        with patch.object(upload, '_claim', side_effect=change):
            self.assertEqual(self.call()['reason'], 'pre_send_validation_failed')
        self.assertEqual(self.writes(), 0)

    def test_native_timestamp_binding_and_baseline_cannot_be_rebound(self):
        original = copy.deepcopy(self.document)
        self.document['request']['userPlaylogList'][0]['userPlayDate'] = '2020-01-01 00:00:00.0'
        self.assert_rejected()
        self.document = copy.deepcopy(original)
        self.snapshot['nodes']['userChargeList'][0]['stock'] += 1
        self.assert_rejected()

    def test_claim_failure_and_no_secret_leak(self):
        with patch.object(upload, '_claim', side_effect=OSError('SECRET path')):
            self.assertEqual(self.call(), dict(status='rejected', reason='claim_failed'))
        self.assertEqual(self.writes(), 0)


if __name__ == '__main__':
    unittest.main()
