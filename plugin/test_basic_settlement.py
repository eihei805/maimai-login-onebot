"""Offline full-schema fixtures, deliberately synthetic account/machine values."""
import copy
import json
from pathlib import Path
import unittest
from basic_settlement import build_basic_settlement, SCHEMA


def zero(typ):
    if isinstance(typ, dict): return []
    if typ in SCHEMA: return {k: zero(t) for k, t in SCHEMA[typ].items()}
    if typ == 'str': return ''
    if typ == 'bool': return False
    if typ in ('int', 'uint', 'long', 'ulong'): return 0
    raise AssertionError(typ)


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.nodes = {k: [zero(t)] for k, t in dict(userData='UserDetail', userExtend='UserExtend', userOption='UserOption', userRatingList='UserRating').items()}
        self.nodes.update(userWeeklyData=zero('UserWeeklyData'), userChargeList=[],
            userFavoriteList=[dict(itemKind=k,itemIdList=[5]) for k in (1,2,3,10,11)], userActivityList=[],
            userMissionDataList=[], userCharacterList=[dict(characterId=5,level=9,awakening=2,useCount=17)],
            userMusicDetailList=[], userItemList=[])
        self.nodes['userData'][0].update(charaSlot=[5]*5, playCount=14, currentPlayCount=3,
            playerRating=1200, musicRating=1200, point=99)
        self.snap=dict(userId=1,loginId=2,loginDateTime=1800000000,capturedAt=1800000010,
            complete=True,itemKinds=[1],nodes=self.nodes,rawFavoriteKinds=[1,2,3,None,11])
        self.machine=dict(utcOffsetSeconds=0,version=15600,placeId=99,placeName='offline',
            playMode=0,beforeGrade=0,afterGradeRank=1,isEventMode=False,isNewFree=False,
            isCriticalDisp=True,isFastLateDisp=True,categoryIndex=1,playCredit=1,
            regionId=1,regionName='offline',clientId='OFFLINE',gameId='SDGB',countryCode='CHN',
            versionString='1.56.00',dataVersion='offline',musicIndex=0,extraFlag=0,
            selectScoreType=0,localPlayCount=1,playSpecial=1024,isFreePlay=False)
        self.kw=dict(user_id=1,login_id=2,login_date_time=1800000000,auth_date_time=1800000000,
            play_date_time=1800000020,settle_date_time=1800000030,now_unix=1800000040,
            catalog=[{'id': '8', 'title': 'True Love Song', 'type': 'SD', 'charts': [{'notes': [63, 23, 8, 2]}]}])

    def build(self): return build_basic_settlement(self.snap,self.machine,**self.kw)

    def test_full_packet_new_chart_and_no_input_mutation(self):
        original=copy.deepcopy((self.snap,self.machine))
        out=self.build(); ua=out['upsertUserAll']; log=out['userPlaylogList'][0]
        self.assertEqual((self.snap,self.machine),original)
        self.assertEqual(len(log),110)
        self.assertEqual(set(ua),set(SCHEMA['UserAll']))
        self.assertEqual([log[k+'Miss'] for k in ('tap','hold','slide','touch','break')],[63,23,8,0,2])
        self.assertEqual(log['totalCombo'],96)
        for k in ('achievement','deluxscore','maxCombo','comboStatus','syncStatus'): self.assertEqual(log[k],0)
        self.assertFalse(log['isClear'])
        self.assertEqual(log['beforeRating'],log['afterRating'])
        self.assertEqual(ua['userData'][0]['playCount'],15)
        self.assertEqual(ua['userData'][0]['point'],99)
        self.assertEqual(ua['userMusicDetailList'][0]['playCount'],1)
        self.assertEqual(ua['isNewMusicDetailList'],'1')
        self.assertEqual(ua['userItemList'],[dict(itemKind=1,itemId=11,stock=1,isValid=True)])
        self.assertEqual(ua['isNewItemList'],'1')
        for k in ('userRatingList','userOption','userWeeklyData','userChargeList','userFavoriteList','userActivityList','userMissionDataList'):
            self.assertEqual(ua[k],self.nodes[k])

    def test_previous_best_preserved_and_existing_item_noop(self):
        old=dict(musicId=8,level=0,playCount=7,achievement=1005000,comboStatus=4,
            syncStatus=3,deluxscoreMax=288,scoreRank=13,extNum1=9)
        self.nodes['userMusicDetailList']=[old]
        self.nodes['userItemList']=[dict(itemKind=1,itemId=11,stock=4,isValid=True)]
        ua=self.build()['upsertUserAll']
        self.assertEqual(ua['userMusicDetailList'],[dict(old,playCount=8)])
        self.assertEqual(ua['isNewMusicDetailList'],'0')
        self.assertEqual(ua['userItemList'],[])
        self.assertEqual(ua['isNewItemList'],'')

    def test_missing_each_machine_field_fails_precisely(self):
        for key in set(self.machine) - {'beforeGrade', 'afterGradeRank', 'isCriticalDisp', 'isFastLateDisp'}:
            value=self.machine.pop(key)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError,key): self.build()
            self.machine[key]=value

    def test_player_fields_override_obsolete_machine_hints(self):
        for key in ('beforeGrade', 'afterGradeRank', 'isCriticalDisp', 'isFastLateDisp'):
            self.machine.pop(key)
        self.nodes['userOption'][0]['dispJudge'] = 5
        self.nodes['userRatingList'][0]['udemae']['classValue'] = 270001
        log = self.build()['userPlaylogList'][0]
        self.assertTrue(log['isCriticalDisp'])
        self.assertTrue(log['isFastLateDisp'])
        self.assertEqual(log['afterGradeRank'], 25)
        self.assertEqual(log['beforeRating'], 1200)

    def test_legacy_rating_requires_native_recalculation(self):
        self.nodes['userData'][0]['gradeRating'] = 1
        with self.assertRaisesRegex(ValueError, 'native rate recalculation'):
            self.build()

    def test_missing_save_fields_and_unknown_fields_fail(self):
        for key in ('userData','userExtend','userOption','userRatingList'):
            first=next(iter(self.nodes[key][0])); value=self.nodes[key][0].pop(first)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError,first): self.build()
            self.nodes[key][0][first]=value
        self.nodes['userData'][0]['unknown']=1
        with self.assertRaisesRegex(ValueError,'unknown'): self.build()

    def test_binding_freshness_scope_and_bool_rejected(self):
        for key,value in [('userId',True),('loginId',7),('complete',False),('capturedAt',1799999999),('itemKinds',[])]:
            old=self.snap[key];self.snap[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.build()
            self.snap[key]=old
        self.kw['now_unix']+=400
        with self.assertRaisesRegex(ValueError,'older than'):self.build()

    def test_incomplete_character_fails(self):
        self.nodes['userCharacterList']=[]
        with self.assertRaisesRegex(ValueError,'character'):self.build()

    def test_catalog_rejected(self):
        self.kw['catalog']=[]
        with self.assertRaisesRegex(ValueError,'catalog'):self.build()

    def test_snapshot_normalization_preserves_and_fails_missing(self):
        from settlement_snapshot import normalize_snapshot
        raw=copy.deepcopy(self.nodes)
        for key in ('userExtend','userOption','userRatingList'):raw[key]=raw[key][0]
        raw['userData']=dict(userId=1,banState=0,userData=raw['userData'][0])
        raw['userData']['userData']['friendCode']='GET only'
        raw['userFavoriteList']=[dict(requestedKind=k,response=dict(itemKind=k,itemIdList=[5])) for k in (1,2,3,10,11)]
        args=dict(user_id=1,login_id=2,login_date_time=1800000000,captured_at=1800000010,
            complete=True,item_kinds=[1],extend_flags=dict(isPhotoAgree=False,isGotoCodeRead=False))
        snap=normalize_snapshot(raw,**args)
        self.assertEqual(snap['nodes'],self.nodes)
        self.assertIn('friendCode',raw['userData']['userData'])
        del raw['userData']['userData']['point']
        with self.assertRaisesRegex(ValueError,'point'):normalize_snapshot(raw,**args)

    def test_duplicate_chart_and_overflow_fail(self):
        row=dict(musicId=8,level=0,playCount=7,achievement=1005000,comboStatus=4,
            syncStatus=3,deluxscoreMax=288,scoreRank=13,extNum1=9)
        self.nodes['userMusicDetailList']=[row,row]
        with self.assertRaisesRegex(ValueError,'duplicate chart'):self.build()
        self.nodes['userMusicDetailList']=[]
        self.nodes['userData'][0]['playCount']=2147483647
        with self.assertRaisesRegex(ValueError,'playCount'):self.build()

    def test_midnight_log_day_and_explicit_offset(self):
        # 2027-01-15 06:00 UTC wall time belongs to previous log day.
        from datetime import datetime,timezone
        t=int(datetime(2027,1,15,6,tzinfo=timezone.utc).timestamp())
        self.machine['utcOffsetSeconds']=28800
        self.snap.update(loginDateTime=t,capturedAt=t-28800+10)
        self.kw.update(login_date_time=t,play_date_time=t+20,settle_date_time=t+30,now_unix=t-28800+40)
        log=self.build()['userPlaylogList'][0]
        self.assertEqual(log['playDate'],'2027-01-14')
        self.assertEqual(log['loginDate'],t-28800)


if __name__=='__main__': unittest.main()
