#!/usr/bin/env python3
"""Opt-in live adapter for the offline session model. Never logs wire values."""
import argparse
import dataclasses
import contextlib
import fcntl
import math
import importlib.util
import http.cookiejar
import json
import os
from pathlib import Path
import re
import signal
import ssl
import stat
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

PRIVATE = Path(os.getenv('MAIMAI_PRIVATE_DIR', str(Path(__file__).resolve().parent / 'private')))
CONFIG = PRIVATE / 'config.json'
spec = importlib.util.spec_from_file_location('maimai_minimal', Path(__file__).with_name('maimai-minimal-client.py'))
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

class SafeFailure(Exception):
    pass

def private_read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600:
            raise SafeFailure('private_file_permissions')
        data = f.read(limit + 1)
    if len(data) > limit:
        raise SafeFailure('private_file_size')
    return data

def private_dir():
    s = PRIVATE.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700:
        raise SafeFailure('private_directory_permissions')

def load_config():
    private_dir()
    raw = m.object_json(private_read(CONFIG, 32768))
    c = dict(raw['config'])
    for key in ('aes_key', 'aes_iv', 'salt', 'qr_secret'):
        c[key] = bytes.fromhex(c[key])
    return m.Config(**c), raw

def blockers(c, raw, allow_http_qr=False):
    found = [b for b in raw.get('blockers', []) if not (allow_http_qr and b == 'qr_endpoint_has_no_proven_tls')]
    for label, url in (('qr', c.qr_url), ('game', c.game_base)):
        p = urllib.parse.urlsplit(url)
        if p.scheme != 'https' and not (label == 'qr' and p.scheme == 'http' and allow_http_qr):
            found.append(label + '_endpoint_has_no_proven_tls')
        if not p.hostname or p.username or p.password or p.query or p.fragment:
            found.append(label + '_endpoint_invalid')
    return sorted(set(found))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SafeFailure('redirect_rejected')

def origin(url):
    p = urllib.parse.urlsplit(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == 'https' else 80)

class GameCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    """Host-only cookies, exact scheme/host/port, never the QR endpoint."""
    def __init__(self, config):
        super().__init__()
        self.game_origin = origin(config.game_base)
        self.qr_url = config.qr_url

    def allowed(self, request):
        return (request.full_url != self.qr_url and
                origin(request.full_url) == self.game_origin)

    def set_ok(self, cookie, request):
        return (self.allowed(request) and not cookie.domain_specified and
                super().set_ok(cookie, request))

    def return_ok(self, cookie, request):
        return self.allowed(request) and super().return_ok(cookie, request)

class Transport:
    def __init__(self, c, timeout=15, total=180, limit=8*1024*1024+65536, allow_http_qr=False, allow_settlement=False, allow_ticket_purchase=False):
        self.allow_ticket_purchase = allow_ticket_purchase
        self._ticket_purchase_used = False
        self.allow_settlement = allow_settlement
        self._settlement_used = False
        self.config, self.timeout, self.limit = c, timeout, limit
        self.deadline = time.monotonic() + total
        self.calls, self.stage, self.last_error = 0, 'none', None
        self.identity = None
        self.allow_http_qr = allow_http_qr
        self._qr_used = False
        self._login_used = False
        self._closed = False
        self.cookies = http.cookiejar.CookieJar(policy=GameCookiePolicy(c))
        # Jar is private to this single-user run; QR is handled without cookies.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self.opener.addheaders = []

    def clear_session(self):
        self.cookies.clear()
        self.identity = None
        self._closed = True

    def __call__(self, req):
        self.stage = req.stage
        self.last_error = None
        try:
            if self._closed:
                raise SafeFailure('session_closed')
            if req.stage == 'UserLoginApi':
                if self._login_used:
                    raise SafeFailure('transport_single_user_only')
                self._login_used = True
            if req.stage == 'UpsertUserChargelogApi':
                if not self.allow_ticket_purchase or self._ticket_purchase_used:
                    raise SafeFailure('ticket_purchase_not_authorized_or_already_attempted')
                self._ticket_purchase_used = True
            if req.stage == 'UpsertUserAllApi':
                if not self.allow_settlement or self._settlement_used:
                    raise SafeFailure('settlement_not_authorized_or_already_attempted')
                self._settlement_used = True
            if req.method != 'POST':
                raise SafeFailure('method_rejected')
            if req.stage == 'QRLookup':
                if self._qr_used:
                    raise SafeFailure('transport_single_user_only')
                self._qr_used = True
                valid = req.url == self.config.qr_url
            else:
                valid = (req.stage == 'UpsertUserAllApi' and self.allow_settlement or req.stage == 'UpsertUserChargelogApi' and self.allow_ticket_purchase or req.stage in ('UserLoginApi', 'GetUserMusicApi', 'GetUserDataApi', 'UserLogoutApi', 'GetUserChargeApi', 'GetGameChargeApi', 'GetUserItemApi', 'GetUserExtendApi', 'GetUserOptionApi', 'GetUserRatingApi', 'GetUserCharacterApi', 'GetUserFavoriteApi', 'GetUserMissionDataApi', 'GetUserActivityApi')) and req.url.startswith(self.config.game_base) and bool(re.fullmatch('[0-9a-f]{32}', req.url[len(self.config.game_base):]))
            scheme = urllib.parse.urlsplit(req.url).scheme
            allowed_scheme = scheme == 'https' or (req.stage == 'QRLookup' and self.allow_http_qr and scheme == 'http')
            if not valid or not allowed_scheme:
                raise SafeFailure('destination_rejected')
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise SafeFailure('total_timeout')
            headers = {k: v for k, v in req.headers.items() if k.lower() not in ('cookie', 'cookie2')}
            if req.stage != 'QRLookup' and (origin(req.url) != origin(self.config.game_base) or req.url == self.config.qr_url):
                raise SafeFailure('destination_rejected')
            if req.stage != 'QRLookup':
                headers['Accept-Encoding'] = ''
                headers['Expect'] = '100-continue'
            if req.stage != 'QRLookup':
                if self.identity is None:
                    raise SafeFailure('identity_unavailable')
                headers['User-Agent'] = headers['User-Agent'].split('#', 1)[0] + '#' + self.identity
            request = urllib.request.Request(req.url, data=req.body, headers=headers, method='POST')
            if req.stage != 'QRLookup':
                self.cookies.add_cookie_header(request)
            self.calls += 1
            with self.opener.open(request, timeout=min(self.timeout, remaining)) as response:
                if response.status != 200:
                    raise SafeFailure('http_status_rejected')
                body = response.read(self.limit + 1)
                if len(body) > self.limit:
                    raise SafeFailure('response_size_limit')
                if req.stage != 'QRLookup':
                    self.cookies.extract_cookies(response, request)
            print(json.dumps({'response_metadata': {'stage': req.stage, 'wire_bytes': len(body)}}), file=sys.stderr)
            if req.stage == 'QRLookup':
                try:
                    lookup = m.object_json(body)
                except m.ProtocolError:
                    raise SafeFailure('qr_decode_failure') from None
                uid = lookup.get('aimeId')
                if not m.integer(uid):
                    uid = lookup.get('userID')
                    if isinstance(uid, str) and len(uid) <= 21 and re.fullmatch('[+-]?[0-9]+', uid):
                        uid = int(uid)
                if m.integer(uid) and type(lookup.get('errorID')) is int and lookup['errorID'] == 0:
                    self.identity = str(uid)
            return m.Response(200, body)
        except SafeFailure as exc:
            self.last_error = str(exc)
            raise
        except urllib.error.HTTPError:
            self.last_error = 'http_status_rejected'
            raise SafeFailure(self.last_error) from None
        except Exception:
            self.last_error = 'network_or_tls_failure'
            raise SafeFailure(self.last_error) from None

def save_result(path, data):
    if path.parent != PRIVATE or path.name in ('config.json', 'qr.txt'):
        raise SafeFailure('result_path_rejected')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, allow_nan=False)
        f.write('\n')

def session_path(owner):
    if not isinstance(owner, str) or not re.fullmatch(r'[1-9][0-9]{4,11}', owner):
        raise SafeFailure('session_owner_rejected')
    private_dir()
    return PRIVATE / ('session-' + owner + '.json')

def write_session(owner, data, *, reserve=False):
    """Fixed per-QQ path only; never persist QR. Unknown records block retries."""
    path = session_path(owner)
    data = dict(data, owner=owner, version=1)
    encoded = m.json_bytes(data)
    if len(encoded) > 65536:
        raise SafeFailure('session_size')
    if reserve:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(encoded); f.flush(); os.fsync(f.fileno())
        return
    previous = read_session(owner)
    if previous['owner'] != owner:
        raise SafeFailure('session_owner_rejected')
    temp = PRIVATE / ('session-tmp-' + uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(encoded); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def read_session(owner):
    data = m.object_json(private_read(session_path(owner), 65536))
    if data.get('owner') != owner or data.get('version') != 1 or data.get('state') not in ('unknown', 'active', 'closed'):
        raise SafeFailure('session_owner_or_state_rejected')
    return data

_owned_lock_fd = None

@contextlib.contextmanager
def session_lock():
    global _owned_lock_fd
    private_dir()
    inherited = os.getenv('MAIMAI_SESSION_LOCK_FD')
    previous_fd = _owned_lock_fd
    shared_fd = int(inherited) if inherited else previous_fd
    fd = os.dup(shared_fd) if shared_fd is not None else os.open(PRIVATE / '.session.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_nlink != 1 or stat.S_IMODE(s.st_mode) != 0o600:
            raise SafeFailure('session_lock_permissions')
        expected = (PRIVATE / '.session.lock').lstat()
        if (s.st_dev, s.st_ino) != (expected.st_dev, expected.st_ino):
            raise SafeFailure('session_lock_identity')
        fcntl.flock(fd, fcntl.LOCK_EX)
        _owned_lock_fd = fd
        yield
    finally:
        _owned_lock_fd = previous_fd
        os.close(fd)

def logout_remaining(record, now=None):
    stamp = record.get('login_success_epoch')
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
        return 0
    return max(0, math.ceil(stamp + 60 - (time.time() if now is None else now)))

def persist_session(owner, client, transport, *, login_success_epoch=None):
    previous = read_session(owner)
    policy = {k: previous[k] for k in ('login_success_epoch', 'auto_logout_attempt_epoch') if k in previous}
    if login_success_epoch is not None and 'login_success_epoch' not in policy:
        policy['login_success_epoch'] = login_success_epoch
    write_session(owner, {'state':'active', 'session':dataclasses.asdict(client.session),
        'inputs':dataclasses.asdict(client._login_inputs),
        'auth_date_time':vars(client).get('auth_date_time', previous.get('auth_date_time')),
        'game_base':client.config.game_base, 'chip_id':client.config.chip_id,
        'cookies':[vars(cookie) for cookie in transport.cookies], **policy})

def restore_session(owner, client, transport):
    data = read_session(owner)
    if data['state'] != 'active' or data.get('game_base') != client.config.game_base or data.get('chip_id') != client.config.chip_id:
        raise SafeFailure('session_unavailable_or_config_changed')
    session = m.Session(**data['session'])
    if not all(m.integer(v) for v in (session.user_id, session.login_id, session.login_date_time)) or not isinstance(session.token, str):
        raise SafeFailure('session_invalid')
    inputs = m.LoginInputs(**data['inputs'])
    if session.user_agent_identity != inputs.user_agent_identity:
        raise SafeFailure('session_invalid')
    cookies = data.get('cookies')
    if not isinstance(cookies, list) or len(cookies) > 128:
        raise SafeFailure('session_cookies_invalid')
    for values in cookies:
        values = dict(values)
        values['rest'] = values.pop('_rest')
        cookie = http.cookiejar.Cookie(**values)
        if cookie.domain_specified or cookie.domain.lstrip('.') != urllib.parse.urlsplit(client.config.game_base).hostname:
            raise SafeFailure('session_cookies_invalid')
        transport.cookies.set_cookie(cookie)
    client.session = session
    auth = data.get('auth_date_time')
    if auth is not None and (type(auth) is not int or auth <= 0):
        raise SafeFailure('session_auth_time_invalid')
    client.auth_date_time = auth
    client._login_inputs = inputs
    client._login_attempted = True
    transport.identity = str(session.user_id)
    transport._qr_used = transport._login_used = True
    return session

def logout_owned(owner, client, transport):
    with session_lock():
        return _logout_owned(owner, client, transport)

def _logout_owned(owner, client, transport):
    record = read_session(owner)
    remaining = logout_remaining(record)
    if record['state'] != 'closed' and remaining:
        raise SafeFailure('logout_wait_' + str(remaining))
    if record['state'] == 'closed':
        # Previous logout was confirmed, but unlink failed/crashed. No replay.
        session_path(owner).unlink()
        return
    session = restore_session(owner, client, transport)
    client.logout(session)  # Strict returnCode==1 confirmation, no QR/login.
    transport.clear_session()
    write_session(owner, {'state':'closed'})
    session_path(owner).unlink()

def self_test():
    c = m.Config('https://example.invalid/qr', 'https://example.invalid/game/',
        'A63E01ABCDE', 'MAID', b'Q'*32, b'fixture', bytes(range(32)), bytes(range(16)),
        'test', 'application/json', {'Content-Type':'application/json'})
    codec = m.Codec(c.aes_key, c.aes_iv)
    seen = []
    def fixture(req):
        seen.append(req.stage)
        if req.stage == 'QRLookup':
            return m.Response(200, m.json_bytes({'errorID':0, 'aimeId':123, 'token':'synthetic'}))
        payload = m.object_json(codec.decode(req.body))
        if req.stage == 'UserLoginApi':
            assert payload['clientId'] == c.chip_id and payload['dateTime'] == payload['loginDateTime']
            reply = {'loginId':1, 'returnCode':1}
        else:
            reply = {'nextIndex':0, 'userMusicList':[]}
        return m.Response(200, codec.encode(m.json_bytes(reply)))
    client = m.Client(c, fixture)
    session = client.login('SGWCMAID'+'X'*12+'A'*64, m.LoginInputs(1,2,''))
    scores = client.scores(session, page_budget=2)
    assert scores.complete and scores.pages == 1 and len(seen) == 3
    # Exercise the real adapter via an in-memory opener; zero network.
    transport = Transport(c)
    class FakeResponse:
        status = 200
        from email.message import Message
        headers = Message()
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def info(self): return self.headers
        def read(self, limit): return m.json_bytes({'errorID':0,'aimeId':123,'token':'synthetic'})
    class FakeOpener:
        def open(self, request, timeout):
            assert request.get_method() == 'POST' and 0 < timeout <= 15
            return FakeResponse()
    transport.opener = FakeOpener()
    transport(m.Request('QRLookup','POST',c.qr_url,{},b'{}'))
    assert transport.identity == '123'
    class GameOpener:
        def open(self, request, timeout):
            assert request.get_header('User-agent') == '0'*32+'#123'
            return FakeResponse()
    transport.opener = GameOpener()
    transport(m.Request('UserLoginApi','POST',c.game_base+'0'*32,{'User-Agent':'0'*32+'#'},b'fixture'))
    transport.opener = FakeOpener()
    transport.limit = 1
    try:
        transport(m.Request('QRLookup','POST',c.qr_url,{},b'{}'))
    except SafeFailure: pass
    else: raise AssertionError()
    transport.limit = 1024
    try:
        transport(m.Request('QRLookup','POST','https://unknown.invalid/',{},b'{}'))
    except SafeFailure: pass
    else: raise AssertionError()
    try:
        NoRedirect().redirect_request(None,None,302,None,None,'https://unknown.invalid/')
    except SafeFailure: pass
    else: raise AssertionError()
    assert 'qr_endpoint_has_no_proven_tls' in blockers(dataclasses.replace(c, qr_url='http://example.invalid/qr'), {})
    print(json.dumps({'status':'synthetic_tests_passed', 'requests_live':0}))

def main():
    # All owned CLI operations share the timer/bot lock, including score reads.
    # logout_owned takes the lock itself; avoid nested flock descriptors.
    if '--session-owner' in sys.argv and '--logout-session' not in sys.argv:
        with session_lock():
            return _main()
    return _main()

def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--self-test', action='store_true')
    mode.add_argument('--live', action='store_true')
    mode.add_argument('--logout-session', action='store_true')
    mode.add_argument('--probe-session', action='store_true')
    mode.add_argument('--ticket-inventory', action='store_true')
    mode.add_argument('--game-charge', action='store_true', help='Read current ticket offerings without login or QR')
    mode.add_argument('--settle-basic', action='store_true')
    mode.add_argument('--upload-typed', action='store_true')
    parser.add_argument('--upload-operation', help='Validated scalar upload operation JSON; no secrets')
    mode.add_argument('--upload-game-scores', action='store_true', help='Submit a private, session-bound native post-play packet and verify scores')
    parser.add_argument('--score-document', type=Path)
    mode.add_argument('--ticket-purchase', '--buy-ticket', dest='ticket_purchase', action='store_true', help='Charge purchase; defaults to retaining stock')
    parser.add_argument('--charge-id', type=int, choices=(2, 3))
    parser.add_argument('--ticket-action', choices=('hold', 'buy_use'))
    parser.add_argument('--settlement-plan', type=Path)
    qr = parser.add_mutually_exclusive_group()
    qr.add_argument('--qr-file', type=Path)
    qr.add_argument('--qr-stdin', action='store_true')
    parser.add_argument('--result', type=Path, default=PRIVATE/'result.json')
    parser.add_argument('--page-budget', type=int, default=128)
    parser.add_argument('--preload-profile', action='store_true', help='Opt in to HDD GetUserDataApi before scores')
    parser.add_argument('--logout', action='store_true', help='Opt in to normal logout for this run after successful login, including read failures')
    parser.add_argument('--session-owner', help='Retain/close the fixed private session for this validated QQ')
    parser.add_argument('--allow-http-qr', action='store_true', help='Explicit consent to original HTTP QR endpoint only')
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    c, raw = load_config()
    blocked = blockers(c, raw, args.allow_http_qr or args.logout_session or args.probe_session or args.ticket_inventory or args.settle_basic or args.ticket_purchase or args.game_charge or args.upload_game_scores or args.upload_typed)
    if args.check or blocked:
        print(json.dumps({'status':'blocked' if blocked else 'ready', 'blockers':blocked, 'requests_live':0}))
        return 2 if blocked else 0
    if args.game_charge:
        if args.session_owner or args.qr_file or args.qr_stdin or args.logout:
            raise SafeFailure('game_charge_inputs_rejected')
        transport = Transport(c, total=30)
        transport.identity = '0'
        try:
            reply = m.Client(c, transport)._game('GetGameChargeApi', {'isAll': False}, '0')
            if 'returnCode' in reply and (type(reply['returnCode']) is not int or reply['returnCode'] != 1):
                raise ValueError('game_charge_rejected')
            rows = reply.get('gameChargeList')
            if type(rows) is not list or len(rows) > 4096 or type(reply.get('length')) is not int or reply['length'] != len(rows):
                raise ValueError('game_charge_schema')
            seen, output = set(), []
            for row in rows:
                if type(row) is not dict:
                    raise ValueError('game_charge_schema')
                for key in ('orderId', 'chargeId', 'price'):
                    if type(row.get(key)) is not int or row[key] < 0:
                        raise ValueError('game_charge_schema')
                if row['chargeId'] in seen:
                    raise ValueError('game_charge_duplicate')
                seen.add(row['chargeId'])
                for key in ('startDate', 'endDate'):
                    if type(row.get(key)) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', row[key]):
                        raise ValueError('game_charge_schema')
                output.append({key: row[key] for key in ('orderId', 'chargeId', 'price', 'startDate', 'endDate')})
        except Exception:
            print(json.dumps({'status': 'indeterminate', 'reason': transport.last_error or 'game_charge_protocol_error', 'requests': transport.calls}))
            return 5
        print(json.dumps({'status': 'ok', 'offerings': output, 'target_listed': {str(cid): cid in seen for cid in (2, 3)}, 'requests': transport.calls}))
        return 0
    if args.logout_session:
        if not args.session_owner or args.qr_file or args.qr_stdin or args.logout:
            raise SafeFailure('logout_inputs_rejected')
        transport = Transport(c, total=60)
        try:
            logout_owned(args.session_owner, m.Client(c, transport), transport)
        except SafeFailure as exc:
            remaining = str(exc).removeprefix('logout_wait_')
            print(json.dumps({'status':'failed', 'logout':'unconfirmed',
                              'remaining_seconds': int(remaining) if remaining.isdigit() else 0}))
            return 3
        except Exception:
            print(json.dumps({'status':'failed', 'logout':'unconfirmed'}))
            return 3
        print(json.dumps({'status':'closed', 'logout':'confirmed'}))
        return 0
    if args.probe_session:
        if not args.session_owner or args.qr_file or args.qr_stdin or args.logout:
            raise SafeFailure('probe_inputs_rejected')
        # Read-only liveness probe. Never writes the record, never logs out.
        dead = frozenset(('Empty HTTP body', 'Empty decoded response', 'Invalid JSON response'))
        transport = Transport(c, total=60)
        try:
            client = m.Client(c, transport)
            session = restore_session(args.session_owner, client, transport)
            client.probe(session)
        except m.ProtocolError as exc:
            # The game answered (HTTP 200, valid AES block) but carried no usable
            # session. That is read-only proof this session is gone.
            if str(exc) in dead:
                print(json.dumps({'status':'dead', 'probe':str(exc)}))
                return 4
            print(json.dumps({'status':'indeterminate', 'probe':str(exc)}))
            return 5
        except SafeFailure as exc:
            # Network/TLS/HTTP-level trouble proves nothing about the session.
            print(json.dumps({'status':'indeterminate', 'probe':str(exc)}))
            return 5
        except Exception:
            print(json.dumps({'status':'indeterminate', 'probe':'unknown'}))
            return 5
        print(json.dumps({'status':'alive', 'probe':'ok'}))
        return 0
    if args.ticket_purchase:
        if not args.session_owner or args.qr_file or args.qr_stdin or args.logout or not args.settlement_plan:
            raise SafeFailure('ticket_purchase_inputs_rejected')
        if args.settlement_plan.parent != PRIVATE or not args.settlement_plan.name.startswith('settlement-plan-'):
            raise SafeFailure('settlement_plan_path_rejected')
        plan = m.object_json(private_read(args.settlement_plan, 65536))
        required_plan = {'owner', 'machine', 'extend_flags', 'auth_date_time'}
        optional_plan = {'kind', 'item_ids', 'ticket'}
        if not required_plan <= set(plan) or set(plan) - required_plan - optional_plan or plan['owner'] != args.session_owner:
            raise SafeFailure('ticket_plan_rejected')
        plan.setdefault('kind', 1)
        plan.setdefault('item_ids', [])
        ticket = plan.get('ticket', {})
        if type(ticket) is not dict:
            raise SafeFailure('ticket_plan_rejected')
        ticket = ticket.copy()
        if args.charge_id is not None:
            if 'charge_id' in ticket and ticket['charge_id'] != args.charge_id:
                raise SafeFailure('conflicting_ticket_target')
            ticket['charge_id'] = args.charge_id
        if args.ticket_action is not None:
            if 'action' in ticket and ticket['action'] != args.ticket_action:
                raise SafeFailure('conflicting_ticket_action')
            ticket['action'] = args.ticket_action
        ticket.setdefault('action', 'hold')
        if (type(ticket) is not dict or set(ticket) != {'charge_id', 'action'} or
                type(ticket['charge_id']) is not int or ticket['charge_id'] not in (2, 3) or
                ticket['action'] not in ('hold', 'buy_use') or
                type(plan['kind']) is not int or plan['kind'] != 1 or
                type(plan['item_ids']) is not list or plan['item_ids'] != []):
            raise SafeFailure('ticket_target_rejected')
        if type(plan['machine']) is not dict or type(plan['extend_flags']) is not dict:
            raise SafeFailure('ticket_plan_rejected')
        if plan['auth_date_time'] != 'session' and (type(plan['auth_date_time']) is not int or plan['auth_date_time'] <= 0):
            raise SafeFailure('settlement_auth_time_required')
        simulated = plan['machine'].get('scenario') == 'sdgb_normal_returning_all_miss'
        offset = 28800 if simulated else plan['machine'].get('utcOffsetSeconds')
        # TicketManager.GetValidDate uses cabinet local time; this protocol is
        # China-local. Reject a contradictory machine clock before collection.
        if type(offset) is not int or offset != 28800:
            raise SafeFailure('ticket_machine_requires_china_time')
        if not simulated and (plan['machine'].get('clientId') != c.chip_id or plan['machine'].get('placeId') != raw.get('place_id') or plan['machine'].get('regionId') != raw.get('region_id')):
            raise SafeFailure('ticket_machine_identity_mismatch')
        if not 1 <= args.page_budget <= 512:
            raise SafeFailure('settlement_page_budget_rejected')
        sys.path.insert(0, str(Path(__file__).with_name('plugin')))
        from settlement_collect import collect_snapshot
        from ticket_settlement import submit_ticket
        transport = Transport(c, total=180, allow_settlement=ticket['action'] == 'buy_use', allow_ticket_purchase=True)
        client = m.Client(c, transport)
        try:
            session = restore_session(args.session_owner, client, transport)
        except FileNotFoundError:
            print(json.dumps({'status': 'rejected', 'reason': 'session_required', 'requests': 0, 'retry_allowed': False}))
            return 3
        auth_date_time = client.auth_date_time if plan['auth_date_time'] == 'session' else plan['auth_date_time']
        if type(auth_date_time) is not int or auth_date_time <= 0:
            raise SafeFailure('session_auth_time_unavailable')
        if ticket['action'] == 'hold':
            from ticket_settlement import collect_hold_snapshot
            snapshot = collect_hold_snapshot(client, session, captured_at=int(time.time()))
        else:
            snapshot = collect_snapshot(client, session, captured_at=int(time.time()),
                extend_flags=plan['extend_flags'], item_kinds=(plan['kind'],), page_budget=args.page_budget)
        if simulated:
            from ticket_machine import resolve_machine
            try:
                plan['machine'] = resolve_machine(plan['machine'], snapshot, c, client._login_inputs)
            except Exception:
                raise SafeFailure('invalid_ticket_simulation_profile') from None
        catalog = json.loads(Path(__file__).with_name('plugin').joinpath('data/music_data.json').read_text())
        stamp = int(time.time()) + offset
        build_args = dict(auth_date_time=auth_date_time, kind=plan['kind'], item_ids=[],
            play_date_time=stamp, settle_date_time=stamp, user_id=session.user_id,
            login_id=session.login_id, login_date_time=session.login_date_time, catalog=catalog)
        result = submit_ticket(client, session, owner=args.session_owner, private=PRIVATE,
            snapshot=snapshot, machine=plan['machine'], build_args=build_args,
            charge_id=ticket['charge_id'], action=ticket['action'])
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'verified' else 3
    if args.upload_typed:
        if not args.session_owner or args.qr_file or args.qr_stdin or args.logout or not args.upload_operation:
            raise SafeFailure('typed_upload_inputs_rejected')
        if len(args.upload_operation) > 4096 or not 1 <= args.page_budget <= 512:
            raise SafeFailure('typed_upload_budget')
        operation = m.object_json(args.upload_operation.encode())
        sys.path.insert(0, str(Path(__file__).with_name('plugin')))
        from upload_commands import validate_operation
        operation = validate_operation(operation)
        profile = m.object_json(private_read(PRIVATE / 'ticket-machine-profile.json', 65536))
        if (type(profile) is not dict or not {'machine', 'extend_flags'} <= set(profile)
                or set(profile) - {'machine', 'extend_flags', 'provenance'}
                or type(profile['machine']) is not dict or type(profile['extend_flags']) is not dict):
            raise SafeFailure('typed_upload_profile_rejected')
        from settlement_collect import collect_snapshot
        from typed_settlement import submit_typed
        transport = Transport(c, total=180, allow_settlement=True)
        client = m.Client(c, transport)
        session = restore_session(args.session_owner, client, transport)
        kinds = (operation['kind'],) if operation['operation'] == 'item' else (1,)
        snapshot = collect_snapshot(client, session, captured_at=int(time.time()),
            extend_flags=profile['extend_flags'], item_kinds=kinds, page_budget=args.page_budget)
        from ticket_machine import resolve_machine
        machine = resolve_machine(profile['machine'], snapshot, c, client._login_inputs)
        catalog = json.loads(Path(__file__).with_name('plugin').joinpath('data/music_data.json').read_text())
        result = submit_typed(client, session, owner=args.session_owner, private=PRIVATE,
            snapshot=snapshot, machine=machine, auth_date_time=client.auth_date_time,
            catalog=catalog, operation=operation)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] in ('verified', 'unchanged') else 3
    if args.upload_game_scores:
        if (not args.session_owner or args.qr_file or args.qr_stdin or args.logout
                or not args.score_document or args.settlement_plan):
            raise SafeFailure('game_score_inputs_rejected')
        if (args.score_document.parent != PRIVATE or
                args.score_document.name != 'game-score-' + args.session_owner + '.json'):
            raise SafeFailure('game_score_path_rejected')
        document = m.object_json(private_read(args.score_document, 8*1024*1024))
        if (document.get('owner') != args.session_owner or document.get('version') != 1
                or type(document.get('extend_flags')) is not dict):
            raise SafeFailure('game_score_document_rejected')
        if not 1 <= args.page_budget <= 512:
            raise SafeFailure('game_score_page_budget_rejected')
        sys.path.insert(0, str(Path(__file__).with_name('plugin')))
        from settlement_collect import collect_snapshot
        from game_score_upload import submit_scores
        transport = Transport(c, total=180, allow_settlement=True)
        client = m.Client(c, transport)
        session = restore_session(args.session_owner, client, transport)
        snapshot = collect_snapshot(client, session, captured_at=int(time.time()),
            extend_flags=document['extend_flags'], item_kinds=(1,), page_budget=args.page_budget)
        result = submit_scores(client, session, owner=args.session_owner, private=PRIVATE,
            snapshot=snapshot, document=document)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'verified' else 3
    if args.settle_basic:
        if not args.session_owner or args.qr_file or args.qr_stdin or args.logout or not args.settlement_plan:
            raise SafeFailure('settlement_inputs_rejected')
        # Locally prepared explicit machine inputs only; never accept chat JSON.
        if args.settlement_plan.parent != PRIVATE or not args.settlement_plan.name.startswith('settlement-plan-'):
            raise SafeFailure('settlement_plan_path_rejected')
        plan = m.object_json(private_read(args.settlement_plan, 65536))
        expected = {'owner', 'machine', 'extend_flags', 'auth_date_time', 'kind', 'item_ids'}
        if set(plan) != expected or plan['owner'] != args.session_owner:
            raise SafeFailure('settlement_plan_rejected')
        if type(plan['machine']) is not dict or type(plan['extend_flags']) is not dict:
            raise SafeFailure('settlement_plan_rejected')
        # This entry point is scoped to the single explicitly authorized test.
        if type(plan['kind']) is not int or plan['kind'] != 1 or plan['item_ids'] != [201] or any(type(v) is not int for v in plan['item_ids']):
            raise SafeFailure('settlement_target_rejected')
        if type(plan['auth_date_time']) is not int or plan['auth_date_time'] <= 0:
            raise SafeFailure('settlement_auth_time_required')
        if not 1 <= args.page_budget <= 512:
            raise SafeFailure('settlement_page_budget_rejected')
        sys.path.insert(0, str(Path(__file__).with_name('plugin')))
        from settlement_collect import collect_snapshot
        from settlement_submit import submit_basic
        transport = Transport(c, total=180, allow_settlement=True)
        client = m.Client(c, transport)
        session = restore_session(args.session_owner, client, transport)
        snapshot = collect_snapshot(client, session, captured_at=int(time.time()),
            extend_flags=plan['extend_flags'], item_kinds=(plan['kind'],), page_budget=args.page_budget)
        catalog = json.loads(Path(__file__).with_name('plugin').joinpath('data/music_data.json').read_text())
        build_args = {k: plan[k] for k in ('auth_date_time', 'kind', 'item_ids')}
        offset = plan['machine'].get('utcOffsetSeconds')
        if type(offset) is not int:
            raise SafeFailure('settlement_offset_required')
        simulated_at = int(time.time()) + offset
        build_args.update(play_date_time=simulated_at, settle_date_time=simulated_at)
        build_args.update(user_id=session.user_id, login_id=session.login_id,
            login_date_time=session.login_date_time, catalog=catalog)
        result = submit_basic(client, session, owner=args.session_owner, private=PRIVATE,
            snapshot=snapshot, machine=plan['machine'], build_args=build_args)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'verified' else 3
    if args.ticket_inventory:
        if not args.session_owner or args.qr_file or args.qr_stdin or args.logout:
            raise SafeFailure('ticket_inputs_rejected')
        # Read-only inventory: one GetUserChargeApi against the retained session.
        # No login, no logout, no write, no clock refresh, no record update.
        transport = Transport(c, total=60)
        try:
            client = m.Client(c, transport)
            session = restore_session(args.session_owner, client, transport)
            rows = client.ticket_inventory(session)
        except SafeFailure as exc:
            print(json.dumps({'status':'indeterminate', 'reason':str(exc)}))
            return 5
        except m.ProtocolError:
            # Client wraps transport exceptions; retain the adapter's safe reason.
            if transport.last_error:
                print(json.dumps({'status':'indeterminate', 'reason':transport.last_error}))
                return 5
            print(json.dumps({'status':'failed', 'reason':'inventory_protocol_error'}))
            return 3
        except Exception:
            print(json.dumps({'status':'indeterminate', 'reason':'unknown'}))
            return 5
        print(json.dumps({'status':'ok', 'tickets':[dict(row) for row in rows]}, ensure_ascii=False))
        return 0
    if not args.live or not (args.qr_file or args.qr_stdin) or not 1 <= args.page_budget <= 512 or (args.session_owner and not args.qr_stdin):
        raise SafeFailure('live_inputs_required')
    # Reserve a new private result before consuming QR or contacting the server.
    if args.result.parent != PRIVATE or args.result.name in ('config.json','qr.txt') or args.result.exists():
        raise SafeFailure('result_path_unavailable')
    save_result(args.result, {'status':'started'})
    if args.session_owner:
        write_session(args.session_owner, {'state':'unknown'}, reserve=True)
    transport = Transport(c, allow_http_qr=args.allow_http_qr)
    def alarm(*unused):
        raise SafeFailure('total_timeout')
    signal.signal(signal.SIGALRM, alarm)
    signal.alarm(180)
    status = {'status':'failed','stage':'input','requests':0,'pages':0,'rows':0}
    client = None
    output = {}
    try:
        qr_data = private_read(args.qr_file, 256) if args.qr_file else sys.stdin.buffer.read(257)
        if len(qr_data) > 256: raise SafeFailure('qr_input_size')
        qr_text = qr_data.decode('ascii').rstrip('\r\n')
        client = m.Client(c, transport)
        session = client.login(qr_text, m.LoginInputs(raw['region_id'], raw['place_id'], ''))
        login_success_epoch = time.time()  # Local epoch captured before profile/score reads.
        if args.session_owner:
            persist_session(args.session_owner, client, transport, login_success_epoch=login_success_epoch)
            status['session'] = 'active'
        try:
            if args.preload_profile:
                status['profile'] = 'failed'
                client.profile(session)  # Validate, then discard all profile data.
                status['profile'] = 'loaded'
            scores = client.scores(session, page_budget=args.page_budget)
            status.update(status='complete' if scores.complete else 'partial', pages=scores.pages,
                          rows=len(scores.rows), reason=scores.reason)
            fields = ('musicId','level','playCount','achievement','deluxscoreMax',
                      'comboStatus','syncStatus','scoreRank','extNum1')
            output['scores'] = [{k: row[k] for k in fields if k in row} for row in scores.rows]
        except Exception:
            status['read_transport_error'] = transport.last_error
            raise
        finally:
            # Reached only after this client has committed a successful login.
            if args.logout and not args.session_owner:
                status['logout'] = 'unconfirmed'
                try:
                    if args.session_owner:
                        # Keep the committed record if logout raises or the
                        # process is interrupted; /logout can restore it.
                        persist_session(args.session_owner, client, transport)
                        logout_owned(args.session_owner, client, transport)
                        status['session'] = 'none'
                    else:
                        client.logout(session)
                        transport.clear_session()
                except Exception:
                    status['logout'] = 'unconfirmed'
                else:
                    status['logout'] = 'confirmed'
            elif args.session_owner:
                # Persist cookies acquired during reads too; failure leaves the
                # earlier committed record for conservative logout recovery.
                persist_session(args.session_owner, client, transport)
    except Exception as exc:
        if transport.stage in (None, 'none', 'QRLookup') and (client is None or client.session is None):
            status['login'] = 'not_started'
            if args.session_owner:
                session_path(args.session_owner).unlink(missing_ok=True)
                status['session'] = 'none'
        status['error'] = status.pop('read_transport_error', transport.last_error) or 'input_or_protocol_failure'
        if isinstance(exc, m.ProtocolError):
            known = {'Invalid loginId; session not committed', 'Login rejected', 'Invalid loginDateTime', 'Empty HTTP body', 'Empty decoded response', 'Profile returnCode rejected', 'Invalid profile envelope', 'Invalid ciphertext length', 'Invalid PKCS7 padding', 'Invalid zlib response', 'Invalid JSON response', 'Expected JSON object'}
            if str(exc) in known:
                status['protocol_error'] = str(exc)
    finally:
        signal.alarm(0)
    status.update(stage=transport.stage, requests=transport.calls)
    if args.session_owner:
        status.setdefault('session', 'unknown')
    output['summary'] = status
    # Already created with O_EXCL, never follow a replaced symlink.
    fd = os.open(args.result, os.O_WRONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'w') as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or stat.S_IMODE(s.st_mode) != 0o600 or s.st_uid != os.getuid():
            raise SafeFailure('result_permissions')
        f.truncate(0)
        json.dump(output, f, ensure_ascii=False, allow_nan=False)
        f.write('\n')
    print(json.dumps(status))
    return 0 if status['status'] == 'complete' and status.get('logout') != 'unconfirmed' else 3

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({'status':'failed', 'error':'local_preflight_or_io_failure'}))
        sys.exit(2)
