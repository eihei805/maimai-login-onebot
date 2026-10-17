"""Single-attempt settlement coordinator; caller holds the shared session lock.

No login, retry, runtime configuration, or secret-bearing results. ``private``
must already exist, belong to this uid, and have mode 0700. Claims are permanent,
including when a process crashes before sending. Do not remove claims to retry.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

from basic_settlement import build_basic_settlement, check, required
from settlement import BITS_TO_ARRAY, inventory
from settlement_snapshot import DROP


def _directory(path):
    # Walk from / using dirfds so symlinks in ANY component are rejected.
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if '..' in path.parts:
        raise ValueError('private_directory_parent_traversal')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('private_directory_required')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _claim_name(binding, owner):
    return hashlib.sha256(json.dumps([owner, *binding[:2]],
                                    separators=(',', ':')).encode()).hexdigest() + '.claim'


def _claimed(private, binding, owner):
    """Read-only early check; _claim's O_EXCL remains the atomic authority."""
    directory = _directory(private)
    try:
        try:
            os.stat(_claim_name(binding, owner), dir_fd=directory, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
    finally:
        os.close(directory)


def _claim(private, binding, owner):
    name = _claim_name(binding, owner)
    directory = _directory(private)
    try:
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
        except FileExistsError:
            return False  # includes symlink entries; never follow or remove them
        try:
            marker = b'settlement-attempt-consumed\n'
            if os.write(fd, marker) != len(marker):
                raise OSError('short claim write')
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(directory)
        return True
    finally:
        os.close(directory)


def _validate(packet, snapshot, machine, binding, clock):
    if type(packet) is not dict or set(packet) != {
        'userId', 'playlogId', 'loginDateTime', 'isEventMode', 'isFreePlay',
        'userPlaylogList', 'upsertUserAll'}:
        raise ValueError('invalid_packet')
    for key, expected in zip(('userId', 'playlogId', 'loginDateTime'), binding):
        if required(packet, key, int) != expected:
            raise ValueError('session_binding')
    for key in ('isEventMode', 'isFreePlay'):
        required(packet, key, bool)
    ua = required(packet, 'upsertUserAll', dict)
    check(ua, 'UserAll', 'upsertUserAll')
    logs = required(packet, 'userPlaylogList', list)
    if len(logs) != 1:
        raise ValueError('invalid_playlogs')
    check(logs[0], 'UserPlaylog', 'playlog')
    if logs[0]['playlogId'] != binding[1] or logs[0]['loginDate'] != binding[2] - machine['utcOffsetSeconds']:
        raise ValueError('playlog_binding')
    if len(ua['userGamePlaylogList']) != 1 or ua['userGamePlaylogList'][0]['playlogId'] != binding[1]:
        raise ValueError('gameplay_binding')
    for bits, array in BITS_TO_ARRAY.items():
        allowed = '12' if bits == 'isNewFavoriteList' else '01'
        if bits == 'isNewFavoritemusicList':
            if ua[bits] not in ('', '0', '1'):
                raise ValueError('invalid_bits')
        elif len(ua[bits]) != len(ua[array]) or any(c not in allowed for c in ua[bits]):
            raise ValueError('invalid_bits')
    if type(clock) not in (int, float) or not math.isfinite(clock):
        raise ValueError('invalid_clock')
    captured = required(snapshot, 'capturedAt', int)
    if not 0 <= clock - captured <= 300:
        raise ValueError('stale_snapshot')
    for key, value in zip(('userId', 'loginId', 'loginDateTime'), binding):
        if required(snapshot, key, int) != value:
            raise ValueError('snapshot_binding')


def _readback(client, session, packet, snapshot, kind, current):
    ua = packet['upsertUserAll']
    checks = dict(items=False, profile=False, chart=False)

    def read(api, fields=None):
        current()
        response = client._game(api, dict(userId=session.user_id, **(fields or {})),
                                session.user_agent_identity)
        current()
        if type(response) is not dict or required(response, 'userId', int) != session.user_id:
            raise ValueError('readback_binding')
        if 'returnCode' in response and required(response, 'returnCode', int) != 1:
            raise ValueError('readback_code')
        return response

    try:
        current()
        actual = inventory(client, session, kind)
        current()
        expected = {(r['itemKind'], r['itemId']): r for r in snapshot['nodes']['userItemList'] if r['itemKind'] == kind}
        expected.update({(r['itemKind'], r['itemId']): r for r in ua['userItemList']})
        checks['items'] = {(r['itemKind'], r['itemId']): r for r in actual} == expected
    except Exception:
        pass
    try:
        reply = read('GetUserDataApi')  # userId-only request, wrapped full profile
        if 'nextIndex' in reply and required(reply, 'nextIndex', int) != 0:
            raise ValueError('profile_continuation')
        profile = deepcopy(required(reply, 'userData', dict))
        ban = required(reply, 'banState', int)
        if 'banState' in profile and profile['banState'] != ban:
            raise ValueError('profile_ban_conflict')
        profile['banState'] = ban
        for key in DROP['userData']:
            profile.pop(key, None)
        check(profile, 'UserDetail', 'profile')
        checks['profile'] = all(profile[key] == ua['userData'][0][key]
                                for key in ('playCount', 'currentPlayCount', 'lastPlayDate', 'lastPlayCredit'))
    except Exception:
        pass
    try:
        cursor, seen, charts = 0, set(), {}
        for _ in range(128):
            if cursor in seen:
                raise ValueError('music_cycle')
            seen.add(cursor)
            reply = read('GetUserMusicApi', dict(nextIndex=cursor, maxCount=50))
            groups = required(reply, 'userMusicList', list)
            count = 0
            for group in groups:
                for row in required(group, 'userMusicDetailList', list):
                    row = deepcopy(row)
                    row.pop('extNum2', None)
                    check(row, 'UserMusicDetail', 'chart')
                    key = (row['musicId'], row['level'])
                    if key in charts:
                        raise ValueError('duplicate_chart')
                    charts[key] = row
                    count += 1
            cursor = required(reply, 'nextIndex', int)
            if cursor == 0:
                expected = ua['userMusicDetailList'][0]
                checks['chart'] = charts.get((8, 0), {}).get('playCount') == expected['playCount']
                break
            if cursor < 0 or not count:
                raise ValueError('music_continuation')
    except Exception:
        pass
    return checks


def submit_basic(client, session, *, owner, private, snapshot, machine, build_args, now=time.time):
    """Build, claim, send once, read back. Caller holds collection/session lock.

    build_args: build_basic_settlement keyword arguments (including explicit
    session ids/times/catalog); now_unix is always overwritten from now().
    owner: nonempty stable string identifying the locked account owner.
    Returns status rejected (+ fixed reason), ambiguous, acknowledged_unverified,
    or verified. Post-send results include boolean checks only, never raw replies.
    verified means inventory + full-profile counters/date + chart count matched;
    it does not attest to every server-side settlement effect.
    """
    try:
        if type(owner) is not str or not owner.strip():
            raise ValueError('owner_required')
        binding = tuple(getattr(session, k) for k in
                        ('user_id', 'login_id', 'login_date_time', 'user_agent_identity'))
        if any(type(v) is not int or v <= 0 for v in binding[:3]) or type(binding[3]) is not str:
            raise ValueError('invalid_session')
        def current():
            client._current(session)
            if binding != tuple(getattr(session, k) for k in
                                ('user_id', 'login_id', 'login_date_time', 'user_agent_identity')):
                raise ValueError('session_changed')
        current()
        snapshot, machine, args = deepcopy(snapshot), deepcopy(machine), deepcopy(build_args)
        args['now_unix'] = int(now())
        packet = build_basic_settlement(snapshot, machine, **args)
        current()
        _validate(packet, snapshot, machine, binding, now())
    except Exception:
        return dict(status='rejected', reason='invalid_or_stale_input')
    try:
        if not _claim(private, binding, owner):
            return dict(status='rejected', reason='already_claimed')
    except Exception:
        return dict(status='rejected', reason='claim_failed')
    try:
        # Claim fsync may block: recheck validity/freshness after it, before send.
        current()
        _validate(packet, snapshot, machine, binding, now())
    except Exception:
        return dict(status='rejected', reason='pre_send_validation_failed')
    try:
        reply = client._game('UpsertUserAllApi', deepcopy(packet), binding[3])
        acknowledged = type(reply) is dict and type(reply.get('returnCode')) is int and reply['returnCode'] == 1
        if acknowledged and 'userId' in reply:
            acknowledged = type(reply['userId']) is int and reply['userId'] == binding[0]
    except Exception:
        acknowledged = False
    checks = _readback(client, session, packet, snapshot, args.get('kind', 1), current)
    status = ('verified' if all(checks.values()) else 'acknowledged_unverified') if acknowledged else 'ambiguous'
    return dict(status=status, checks=checks)
