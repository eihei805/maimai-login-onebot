import unittest
from types import SimpleNamespace
from unittest.mock import patch
from ticket_machine import encode_special, resolve_machine
from test_basic_settlement import BuilderTests

class MachineTests(unittest.TestCase):
    def test_simulation_derives_identity_ui_and_versions(self):
        fixture=BuilderTests(); fixture.setUp()
        profile=dict(scenario='sdgb_normal_returning_all_miss', romVersion=[1,55,3],
            dataVersion='1.55.03',placeName='simulation',regionName='simulation',countryCode='CHN',
            playCredit=1,isFreePlay=True,localPlayCount=0,specialMask=1024)
        config=SimpleNamespace(chip_id='synthetic')
        inputs=SimpleNamespace(place_id=9,region_id=8)
        with patch('ticket_machine.secrets.randbelow',return_value=0):
            machine=resolve_machine(profile,fixture.snap,config,inputs)
        self.assertEqual(machine['version'],1055003)
        self.assertEqual(machine['versionString'],'1.55.03')
        self.assertEqual(machine['clientId'],'synthetic')
        self.assertEqual(machine['placeId'],9)
        self.assertEqual(machine['categoryIndex'],fixture.nodes['userExtend'][0]['categoryIndex'])
        fixture.machine=machine
        fixture.snap['loginDateTime']+=28800
        fixture.kw.update(login_date_time=fixture.snap['loginDateTime'],
            play_date_time=1800000020+28800,settle_date_time=1800000030+28800)
        fixture.build()

    def test_special_encoder_roundtrip(self):
        for mask in (1024,1535):
            for r in (1,1037932):
                value=encode_special(mask,r)
                decoded=int(f'{value & 0xffffffff:032b}'[::-1],2)
                self.assertEqual(decoded,2069*r+mask)
        for mask in (True,0,2048,1536):
            with self.assertRaises(ValueError): encode_special(mask)
