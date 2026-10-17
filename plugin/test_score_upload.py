"""Synthetic only: no network, game requests, private files, or credentials."""
import copy
import json
import unittest
from unittest.mock import patch, MagicMock
import score_upload as u

FISH = [{'id': '8', 'title': 'Synthetic', 'type': 'SD', 'charts': [{'notes': [100, 0, 0, 0]}]},
        {'id': '10008', 'title': 'Synthetic', 'type': 'DX', 'charts': [{'notes': [100, 0, 0, 0]}]}]
LXNS = [{'id': 8, 'title': 'Synthetic', 'difficulties': {'standard': [{'difficulty': 0}], 'dx': [{'difficulty': 0}]}}]
ROW = dict(musicId=10008, level=0, achievement=1001234, deluxscoreMax=299, comboStatus=2, syncStatus=5)
CRED = u.Credentials('synthetic-fish', 'synthetic-personal', lxns_friend_code=123456789)

class Fake:
    def __init__(self, answers): self.answers, self.calls = list(answers), []
    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        value = self.answers.pop(0)
        if isinstance(value, Exception): raise value
        status, data = value
        return status, {}, json.dumps(data).encode()

def lx(data): return (200, {'success': True, 'code': 200, 'data': data})

class Diagnostics(unittest.TestCase):
    def test_personal_player_404_is_specific(self):
        transport = Fake([(404, {})])
        with self.assertLogs('score_upload', level='WARNING'):
            result = u.upload_scores([ROW], FISH, LXNS,
                u.Credentials(lxns_personal_token='synthetic-personal'),
                complete=True, transport=transport)
        self.assertEqual(result['lxns']['reason'], 'lxns_player_not_found')
        self.assertEqual(len(transport.calls), 1)

    def test_scores_404_remains_http_failure(self):
        with self.assertRaisesRegex(u.UploadError, 'http_failure_unconfirmed'):
            u._call(Fake([(404, {})]), 'GET', u.LXNS+'user/maimai/player/scores', {})

    def test_safe_read_failure_log(self):
        transport = Fake([RuntimeError('SECRET upstream response')])
        with self.assertLogs('score_upload', level='WARNING') as logs:
            result = u.upload_scores([ROW], FISH, LXNS,
                u.Credentials(lxns_personal_token='synthetic-personal'),
                complete=True, transport=transport)
        self.assertEqual(result['lxns']['reason'], 'read_failed')
        text = ' '.join(logs.output)
        self.assertIn('stage=read_player reason=read_failed', text)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('synthetic-personal', text)

class Conversion(unittest.TestCase):
    def test_catalog_dx_and_scale(self):
        p = u.prepare_scores([ROW, dict(ROW, musicId=8)], FISH, LXNS)
        self.assertEqual(p.fish[0]['type'], 'DX')
        self.assertEqual(p.lxns[0]['id'], 8)
        self.assertEqual(p.lxns[1]['type'], 'standard')
        self.assertEqual(p.fish[0]['achievements'], 100.1234)
        self.assertEqual(p.fish[0]['fc'], 'fcp')
        self.assertEqual(p.fish[0]['fs'], 'sync')
        self.assertNotIn('scoreRank', p.fish[0])
        self.assertEqual(p.omitted_badges, 0)
    def test_all_combo_constants(self):
        for n, flag in enumerate(u.FC):
            p = u.prepare_scores([dict(ROW, comboStatus=n, syncStatus=5)], FISH, LXNS)
            self.assertEqual(p.fish[0].get('fc', ''), flag)
            self.assertEqual(p.fish[0]['fs'], 'sync')
    def test_unknown_badges_omit(self):
        for n in [-1, 1, 2, 3, 4, 6, True, '5', None]:
            p = u.prepare_scores([dict(ROW, syncStatus=n)], FISH, LXNS)
            self.assertNotIn('fs', p.fish[0])
            self.assertGreaterEqual(p.omitted_badges, 1)
    def test_bad_rows(self):
        for key, values in {'musicId': [True, 0, 10009, 100000], 'level': [5, -1, True],
                            'achievement': [True, 1010001, -1, 1.5, float('nan')],
                            'deluxscoreMax': [-1, True, 301, '3']}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(u.UploadError):
                    u.prepare_scores([dict(ROW, **{key: value})], FISH, LXNS)
        for rows in [[], None]:
            with self.assertRaises(u.UploadError): u.prepare_scores(rows, FISH, LXNS)
        # Duplicate charts are a row-level failure: the first wins, the rest are
        # skipped and counted instead of discarding the whole batch.
        duplicate = u.prepare_scores([ROW, ROW], FISH, LXNS)
        self.assertEqual((len(duplicate.fish), duplicate.skipped_rows), (1, 1))
        self.assertEqual(duplicate.skipped_reasons, {'重复谱面': 1})
    def test_utage_and_high_ids_are_skipped_not_fatal(self):
        # Real catalogs carry utage charts above 99999 (observed max 161852) whose
        # difficulty index is 10, i.e. outside the playable 0..4 range.
        utage = dict(ROW, musicId=10008, level=10)
        p = u.prepare_scores([ROW, utage], FISH, LXNS)
        self.assertEqual((len(p.fish), p.skipped_rows), (1, 1))
        self.assertEqual(p.skipped_reasons, {'不支持的难度': 1})
        # A high ID bound no longer rejects an otherwise valid row by itself.
        FISH_HIGH = [{'id': '111359', 'title': 'High', 'type': 'DX', 'charts': [{'notes': [100, 0, 0, 0]}]}]
        LXNS_HIGH = [{'id': 111359, 'title': 'High', 'difficulties': {'dx': [{'difficulty': 0}]}}]
        high = u.prepare_scores([dict(ROW, musicId=111359, level=0)], FISH_HIGH, LXNS_HIGH)
        self.assertEqual((len(high.fish), high.skipped_rows), (1, 0))
        self.assertEqual(high.fish[0]['title'], 'High')
    def test_unmatchable_rows_report_fixed_labels_only(self):
        # Unknown songs must be skipped with a fixed Chinese label; no title or ID echo.
        p = u.prepare_scores([ROW, dict(ROW, musicId=999999)], FISH, LXNS)
        self.assertEqual((len(p.fish), p.skipped_rows), (1, 1))
        self.assertEqual(p.skipped_reasons, {'曲目未能唯一匹配': 1})
        self.assertNotIn('999999', str(p.skipped_reasons))
    def test_all_rows_skipped_is_blocked(self):
        # Nothing convertible left must still fail closed rather than upload an empty batch.
        with self.assertRaises(u.UploadError) as caught:
            u.prepare_scores([dict(ROW, musicId=999999), dict(ROW, level=10)], FISH, LXNS)
        self.assertEqual(str(caught.exception), 'no_played_scores')
    def test_no_modulo_no_title_guess(self):
        with self.assertRaises(u.UploadError): u.prepare_scores([ROW], FISH[:1], LXNS)
        with self.assertRaises(u.UploadError): u.prepare_scores([ROW], FISH, LXNS*2)
        with self.assertRaises(u.UploadError): u.prepare_scores([ROW], FISH+[dict(FISH[1], id='20008')], LXNS)
    def test_no_mutation(self):
        before = copy.deepcopy((ROW, FISH, LXNS))
        u.prepare_scores([ROW], FISH, LXNS)
        self.assertEqual(before, (ROW, FISH, LXNS))

class Sender(unittest.TestCase):
    def run_upload(self, fake, credentials=CRED, **kw):
        return u.upload_scores([ROW], FISH, LXNS, credentials, complete=True, transport=fake, **kw)
    def test_both_accepted_not_verified_and_headers(self):
        fake = Fake([(200, {'records': []}), (200, {'updates': 0, 'creates': 1}),
                     lx({'friend_code': 123456789}), lx([]), lx(None)])
        r = self.run_upload(fake)
        self.assertEqual([r[s]['state'] for s in ('fish', 'lxns')], ['accepted_unverified']*2)
        self.assertEqual(len(fake.calls), 5)
        self.assertEqual(fake.calls[0][2], {'Import-Token': 'synthetic-fish'})
        self.assertEqual(fake.calls[2][2], {'X-User-Token': 'synthetic-personal'})
        self.assertEqual(fake.calls[1][3][0]['fs'], 'sync')
    def test_preserve_previous_badges_and_scores(self):
        p = u.prepare_scores([ROW], FISH, LXNS)
        old = dict(p.fish[0], fc='app', fs='fsdp', achievements=101, dxScore=300)
        fake = Fake([(200, {'records': [old]}), (200, {'updates': 1, 'creates': 0})])
        r = self.run_upload(fake, u.Credentials(fish_import_token='synthetic'))
        sent = fake.calls[1][3][0]
        self.assertEqual((sent['fc'], sent['fs'], sent['achievements'], sent['dxScore']), ('app','fsdp',101,300))
        self.assertEqual(r['fish']['state'], 'accepted_unverified')
    def test_partial_failure_and_no_secrets(self):
        fake = Fake([(401, {'secret': 'DO-NOT-PRINT'}), lx({'friend_code': 123456789}), lx([]), lx(None)])
        r = self.run_upload(fake)
        self.assertEqual(r['fish']['state'], 'failed'); self.assertEqual(r['lxns']['state'], 'accepted_unverified')
        self.assertNotIn('DO-NOT-PRINT', str(r)); self.assertNotIn('synthetic', repr(CRED))
    def test_redirect_429_exception_and_count_mismatch(self):
        for response in [(302, {}), (429, {}), RuntimeError('TOKEN'), (200, {'updates': 0, 'creates': 0})]:
            pre = [(200, {'records': []})] if isinstance(response, tuple) and response[0] == 200 else []
            fake = Fake(pre+[response])
            r = self.run_upload(fake, u.Credentials(fish_import_token='synthetic'))
            self.assertEqual(r['fish']['state'], 'failed'); self.assertNotIn('TOKEN', str(r))
            self.assertEqual(len(fake.calls), len(pre)+1)
    def test_wrong_friend_blocks_write(self):
        fake = Fake([lx({'friend_code': 999})])
        r = self.run_upload(fake, u.Credentials(lxns_personal_token='synthetic', lxns_friend_code=123))
        self.assertEqual(r['lxns']['reason'], 'friend_code_mismatch')
        self.assertEqual(len(fake.calls), 1)
    def test_personal_identity_without_bound_friend(self):
        fake = Fake([lx({'friend_code': 123}), lx([]), lx(None)])
        r = self.run_upload(fake, u.Credentials(lxns_personal_token='synthetic'))
        self.assertEqual(r['lxns']['state'], 'accepted_unverified')
        self.assertEqual(len(fake.calls), 3)
    def test_invalid_personal_identity_no_write(self):
        for value in (None, True, '123', 0, -1, 10**20):
            fake = Fake([lx({'friend_code': value})])
            r = self.run_upload(fake, u.Credentials(lxns_personal_token='synthetic'))
            self.assertEqual(r['lxns']['reason'], 'invalid_player_identity')
            self.assertEqual(len(fake.calls), 1)
    def test_developer_route_raw_auth(self):
        fake = Fake([lx([]), lx(None)])
        r = self.run_upload(fake, u.Credentials(lxns_developer_token='synthetic-dev', lxns_friend_code=123))
        self.assertEqual(fake.calls[0][1], u.LXNS+'maimai/player/123/scores')
        self.assertEqual(fake.calls[0][2], {'Authorization': 'synthetic-dev'})
        self.assertEqual(r['lxns']['state'], 'accepted_unverified')
    def test_gate_empty_and_unbound(self):
        fake = Fake([])
        self.assertEqual(u.upload_scores([ROW], FISH, LXNS, CRED, transport=fake)['fish']['state'], 'blocked')
        self.assertEqual(self.run_upload(fake, u.Credentials())['fish']['state'], 'not_configured')
        self.assertEqual(fake.calls, [])
    def test_unknown_old_badge_blocks_without_clearing(self):
        old = dict(u.prepare_scores([ROW], FISH, LXNS).fish[0], fs='future')
        fake = Fake([(200, {'records': [old]})])
        r = self.run_upload(fake, u.Credentials(fish_import_token='synthetic'))
        self.assertEqual(r['fish']['reason'], 'unknown_existing_badge'); self.assertEqual(len(fake.calls), 1)
    def test_complete_190_rows(self):
        fish, lxns, rows = [], [], []
        for i in range(1, 191):
            fish.append(dict(FISH[0], id=str(i), title=f'Synthetic{i}'))
            lxns.append(dict(LXNS[0], id=i, title=f'Synthetic{i}'))
            rows.append(dict(ROW, musicId=i))
        fake = Fake([(200, {'records': []}), (200, {'updates': 100, 'creates': 90})])
        r = u.upload_scores(rows, fish, lxns, u.Credentials(fish_import_token='synthetic'), complete=True, transport=fake)
        self.assertEqual(r['fish']['submitted'], 190)
    def test_transport_guards_and_tls(self):
        for url in ['http://www.diving-fish.com/', u.FISH+'records?x=1', 'https://evil.invalid/']:
            with self.assertRaises(u.UploadError): u.https_transport('GET', url, {}, None)
        with patch.object(u.urllib.request, 'build_opener') as factory:
            response = factory.return_value.open.return_value.__enter__.return_value
            response.status=200; response.headers={}; response.read.side_effect=[b'{}', b'']
            u.https_transport('GET', u.FISH+'records', {}, None)
            self.assertEqual(factory.return_value.open.call_args.kwargs['timeout'],20)
            handlers=factory.call_args.args
            self.assertEqual(handlers[0].proxies,{})
            self.assertIsNone(handlers[1].redirect_request(None,None,302,'',{},'https://evil.invalid/'))

if __name__ == '__main__': unittest.main()
