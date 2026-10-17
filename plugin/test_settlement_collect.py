"""Offline contract tests: no production config/session/transport imports."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from basic_settlement import build_basic_settlement
from settlement_collect import collect_snapshot
from test_basic_settlement import BuilderTests


class FakeClient:
    def __init__(self, replies):
        self.session = SimpleNamespace(user_id=1, login_id=2,
            login_date_time=1800000000, user_agent_identity='OFFLINE')
        self.replies, self.calls = replies, []

    def _current(self, session):
        if session is not self.session:
            raise ValueError('session mismatch')

    def _game(self, api, body, identity):
        assert identity == 'OFFLINE'
        assert api.startswith('GetUser') and api.endswith('Api')
        assert body['userId'] == 1
        self.calls.append((api, deepcopy(body)))
        result = self.replies[api]
        if callable(result):
            return result(body)
        return deepcopy(result)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        fixture = BuilderTests()
        fixture.setUp()
        self.fixture = fixture
        nodes = deepcopy(fixture.nodes)
        self.replies = {
            'GetUserDataApi': dict(userId=1, banState=0, userData=nodes['userData'][0]),
            'GetUserExtendApi': dict(userId=1, userExtend=nodes['userExtend'][0]),
            'GetUserOptionApi': dict(userId=1, userOption=nodes['userOption'][0]),
            'GetUserRatingApi': dict(userId=1, userRating=nodes['userRatingList'][0]),
            'GetUserMissionDataApi': dict(userId=1, userWeeklyData=nodes['userWeeklyData'], userMissionDataList=[]),
            'GetUserChargeApi': dict(userId=1, length=0, userChargeList=[]),
            'GetUserActivityApi': dict(userActivity=dict(playList=[], musicList=[])),
            'GetUserFavoriteApi': lambda b: dict(userId=1, userFavorite=dict(itemKind=b['itemKind'], itemIdList=[])),
            'GetUserCharacterApi': dict(userId=1, userCharacterList=nodes['userCharacterList']),
            'GetUserMusicApi': dict(userId=1, nextIndex=0, userMusicList=[]),
            'GetUserItemApi': lambda b: dict(userId=1, itemKind=b['nextIndex']//10**10, nextIndex=0, userItemList=[]),
        }
        self.client = FakeClient(self.replies)
        self.kw = dict(captured_at=1800000010,
                       extend_flags=dict(isPhotoAgree=False, isGotoCodeRead=False))

    def collect(self, **kwargs):
        return collect_snapshot(self.client, self.client.session, **dict(self.kw, **kwargs))

    def test_complete_snapshot_builds_authorized_plate_201_and_song8(self):
        original = deepcopy(self.replies['GetUserDataApi'])
        snap = self.collect()
        out = build_basic_settlement(snap, self.fixture.machine, **self.fixture.kw, item_ids=[201])
        self.assertEqual(out['upsertUserAll']['userItemList'], [dict(itemKind=1,itemId=201,stock=1,isValid=True)])
        self.assertEqual(out['userPlaylogList'][0]['musicId'], 8)
        self.assertEqual(out['userPlaylogList'][0]['level'], 0)
        self.assertEqual(self.replies['GetUserDataApi'], original)
        self.assertTrue(snap['complete'])
        for api, body in self.client.calls:
            expected = {'userId'}
            if api in ('GetUserMusicApi', 'GetUserItemApi'): expected |= {'nextIndex', 'maxCount'}
            if api == 'GetUserFavoriteApi': expected |= {'itemKind'}
            self.assertEqual(set(body), expected, api)
        items = [b for a,b in self.client.calls if a == 'GetUserItemApi']
        self.assertEqual(items, [dict(userId=1,nextIndex=10**10,maxCount=100)])

    def test_music_and_item_pages_preserve_fields(self):
        music = dict(musicId=8,level=0,playCount=7,achievement=1005000,
            comboStatus=4,syncStatus=3,deluxscoreMax=288,scoreRank=13,extNum1=9,extNum2=3)
        self.replies['GetUserMusicApi'] = lambda b: dict(userId=1,nextIndex=50 if b['nextIndex']==0 else 0,
            userMusicList=[dict(userMusicDetailList=[music])] if b['nextIndex']==0 else [])
        self.replies['GetUserItemApi'] = lambda b: dict(userId=1,itemKind=1,
            nextIndex=10**10+1 if b['nextIndex']==10**10 else 0,
            userItemList=[dict(itemKind=1,itemId=201,stock=3,isValid=True)] if b['nextIndex']==10**10 else [])
        snap = self.collect(page_budget=2)
        self.assertEqual(snap['nodes']['userMusicDetailList'], [{k:v for k,v in music.items() if k!='extNum2'}])
        self.assertEqual(snap['nodes']['userItemList'][0]['stock'],3)
        self.assertEqual(len([a for a,b in self.client.calls if a=='GetUserMusicApi']),2)

    def test_each_account_echo_rejected(self):
        for api, reply in list(self.replies.items()):
            if not isinstance(reply,dict): continue
            old = deepcopy(reply)
            reply['userId'] = 9
            with self.subTest(api=api), self.assertRaisesRegex(ValueError,'account mismatch'): self.collect()
            self.replies[api] = old

    def test_explicit_null_access_code_preserved(self):
        self.replies['GetUserDataApi']['userData']['accessCode'] = None
        snapshot = self.collect()
        self.assertIsNone(snapshot['nodes']['userData'][0]['accessCode'])
        del self.replies['GetUserDataApi']['userData']['accessCode']
        with self.assertRaisesRegex(ValueError, 'accessCode'):
            self.collect()

    def test_missing_flags_fail_before_requests(self):
        with self.assertRaisesRegex(ValueError,'isPhotoAgree'): self.collect(extend_flags={})
        self.assertEqual(self.client.calls,[])

    def test_documented_character_display_fields_are_removed(self):
        row = self.replies['GetUserCharacterApi']['userCharacterList'][0]
        row.update(nextAwake=1,nextAwakePercent=2,point=3)
        result = self.collect()
        self.assertEqual(set(result['nodes']['userCharacterList'][0]),
                         {'characterId', 'level', 'awakening', 'useCount'})
        row['unknownSaveField'] = 1
        with self.assertRaisesRegex(ValueError,'unknownSaveField'): self.collect()

    def test_missing_weekly_and_ban_are_not_synthesized(self):
        del self.replies['GetUserMissionDataApi']['userWeeklyData']
        with self.assertRaisesRegex(ValueError,'userWeeklyData'): self.collect()
        self.setUp()
        del self.replies['GetUserDataApi']['banState']
        with self.assertRaisesRegex(ValueError,'banState'): self.collect()

    def test_paging_cycles_empty_continuation_and_budget(self):
        row = dict(itemKind=1,itemId=201,stock=1,isValid=True)
        for nxt, page, message in [(10**10,[row],'cycle'), (10**10+1,[],'empty continuation'),
                                    (2*10**10,[row],'cursor kind'), (True,[row],'nextIndex')]:
            self.replies['GetUserItemApi'] = dict(userId=1,itemKind=1,nextIndex=nxt,userItemList=page)
            with self.subTest(nxt=nxt,page=page), self.assertRaisesRegex(ValueError,message): self.collect()
        self.replies['GetUserItemApi'] = dict(userId=1,itemKind=1,nextIndex=10**10+1,userItemList=[row])
        with self.assertRaisesRegex(ValueError,'page budget'): self.collect(page_budget=1)
        with self.assertRaisesRegex(ValueError,'row budget'): self.collect(row_budget=1)

    def test_duplicate_rows_and_scope_mismatch(self):
        row = dict(itemKind=1,itemId=201,stock=1,isValid=True)
        self.replies['GetUserItemApi'] = dict(userId=1,itemKind=1,nextIndex=0,userItemList=[row,row])
        with self.assertRaisesRegex(ValueError,'duplicate identity'): self.collect()
        self.replies['GetUserItemApi']['itemKind'] = 2
        with self.assertRaisesRegex(ValueError,'kind mismatch'): self.collect()

    def test_character_continuation_is_not_guessed(self):
        self.replies['GetUserCharacterApi']['nextIndex']=1
        with self.assertRaisesRegex(ValueError,'unresolved singleton continuation'): self.collect()

    def test_null_favorite_kind_preserved_and_wrong_kind_rejected(self):
        self.replies['GetUserFavoriteApi'] = dict(userId=1,userFavorite=dict(itemKind=None,itemIdList=[]))
        self.assertEqual(self.collect()['rawFavoriteKinds'],[None]*5)
        self.replies['GetUserFavoriteApi']['userFavorite']['itemKind']=2
        with self.assertRaisesRegex(ValueError,'kind mismatch'): self.collect()

    def test_no_retry_and_no_partial_on_transport_failure(self):
        def fail(body): raise OSError('offline simulated failure')
        self.replies['GetUserMusicApi']=fail
        with self.assertRaises(OSError): self.collect()
        self.assertEqual(len([a for a,b in self.client.calls if a=='GetUserMusicApi']),1)
        self.assertFalse(any(a=='GetUserItemApi' for a,b in self.client.calls))

    def test_session_switch_mid_request_rejected(self):
        def switch(body):
            self.client.session=deepcopy(self.client.session)
            return dict(userId=1,banState=0,userData={})
        self.replies['GetUserDataApi']=switch
        with self.assertRaisesRegex(ValueError,'session mismatch'): self.collect()

    def test_negative_code_nested_account_and_length_rejected(self):
        self.replies['GetUserDataApi']['returnCode']=0
        with self.assertRaisesRegex(ValueError,'returnCode'): self.collect()
        self.setUp()
        self.replies['GetUserMissionDataApi']['userWeeklyData']['userId']=2
        with self.assertRaisesRegex(ValueError,'nested account mismatch'): self.collect()
        self.setUp()
        self.replies['GetUserChargeApi']['length']=1
        with self.assertRaisesRegex(ValueError,'length mismatch'): self.collect()


if __name__ == '__main__': unittest.main()
