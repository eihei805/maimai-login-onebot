"""Resolve an explicitly chosen simulated SDGB normal returning-player scenario.

This describes simulated cabinet state, not detected real cabinet hardware.
Version/shop labels/credit remain explicit profile choices. Account UI state and
configured login identities are reused rather than requested a second time.
"""
import secrets
from basic_settlement import required


def encode_special(mask, random_value=None):
    if type(mask) is not int or mask < 0 or mask & ~1535 or not mask & 1024:
        raise ValueError('invalid_special_mask')
    r = secrets.randbelow(1037932) + 1 if random_value is None else random_value
    if type(r) is not int or not 1 <= r <= 1037932:
        raise ValueError('invalid_special_random')
    raw = 2069*r + mask
    encoded = int(f'{raw & 0xffffffff:032b}'[::-1], 2)
    return encoded if encoded < 2**31 else encoded - 2**32


def resolve_machine(profile, snapshot, config, login_inputs):
    if profile.get('scenario') != 'sdgb_normal_returning_all_miss':
        return profile.copy()
    expected = {'scenario', 'romVersion', 'dataVersion', 'placeName', 'regionName',
                'countryCode', 'playCredit', 'isFreePlay', 'localPlayCount', 'specialMask'}
    if set(profile) != expected:
        raise ValueError('invalid_simulated_machine_profile')
    version = required(profile, 'romVersion', list)
    if len(version) != 3 or any(type(v) is not int or not 0 <= v <= 999 for v in version):
        raise ValueError('invalid_rom_version')
    major, minor, release = version
    extend, = snapshot['nodes']['userExtend']
    return dict(utcOffsetSeconds=28800, version=major*1000000+minor*1000+release,
        versionString=f'{major}.{minor:02d}.{release:02d}', dataVersion=required(profile,'dataVersion',str),
        placeId=login_inputs.place_id, regionId=login_inputs.region_id, clientId=config.chip_id,
        placeName=required(profile,'placeName',str), regionName=required(profile,'regionName',str),
        countryCode=required(profile,'countryCode',str), gameId='SDGB', playMode=0,
        isEventMode=False, isNewFree=False, extraFlag=0, selectScoreType=0,
        categoryIndex=required(extend,'categoryIndex',int), musicIndex=required(extend,'musicIndex',int),
        playCredit=required(profile,'playCredit',int), isFreePlay=required(profile,'isFreePlay',bool),
        localPlayCount=required(profile,'localPlayCount',int),
        playSpecial=encode_special(required(profile,'specialMask',int)))
