#!/usr/bin/env python3
"""Offline protocol model. Only an explicitly injected, local callback handles requests.
No network implementation, secret discovery, environment configuration or target execution.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sys
import zlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class ProtocolError(ValueError):
    """Messages deliberately exclude request, response and secret values."""


def require(ok, message):
    if not ok:
        raise ProtocolError(message)


def integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**64 - 1


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def object_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    try:
        obj = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError('Nonfinite JSON')))
    except (UnicodeError, ValueError, TypeError):
        raise ProtocolError('Invalid JSON response') from None
    require(type(obj) is dict, 'Expected JSON object')
    return obj


@dataclass(frozen=True, repr=False)
class Config:
    qr_url: str
    game_base: str
    chip_id: str
    open_game_id: str
    qr_secret: bytes
    salt: bytes
    aes_key: bytes
    aes_iv: bytes
    mai_encoding: str
    game_content_type: str
    qr_headers: dict

    def __post_init__(self):
        require(isinstance(self.chip_id, str) and re.fullmatch(r'A63E01[A-Z0-9]{5}', self.chip_id), 'Invalid chip shape')
        require(all(isinstance(x, str) and x for x in (self.qr_url, self.game_base, self.open_game_id,
                    self.mai_encoding, self.game_content_type)), 'Missing explicit string configuration')
        require(type(self.qr_secret) is bytes and len(self.qr_secret) == 32, 'QR secret must be 32 bytes')
        require(type(self.salt) is bytes and bool(self.salt), 'Explicit salt required')
        require(type(self.aes_key) is bytes and len(self.aes_key) == 32, 'AES key must be 32 bytes')
        require(type(self.aes_iv) is bytes and len(self.aes_iv) == 16, 'AES IV must be 16 bytes')
        require(type(self.qr_headers) is dict, 'Explicit QR header map required')
        validate_headers(self.qr_headers)
        validate_headers({'Mai-Encoding': self.mai_encoding, 'Content-Type': self.game_content_type})


def validate_headers(headers):
    require(all(isinstance(k, str) and k and isinstance(v, str) and
                '\r' not in k+v and '\n' not in k+v for k, v in headers.items()), 'Invalid headers')


@dataclass(frozen=True, repr=False)
class LoginInputs:
    region_id: int
    place_id: int
    user_agent_identity: str

    def __post_init__(self):
        require(all(integer(n) for n in (self.region_id, self.place_id)), 'Invalid login numeric input')
        require(isinstance(self.user_agent_identity, str), 'Explicit User-Agent identity required')
        validate_headers({'identity': self.user_agent_identity})


@dataclass(frozen=True, repr=False)
class Request:
    stage: str
    method: str
    url: str
    headers: dict
    body: bytes


@dataclass(frozen=True, repr=False)
class Response:
    status: int
    body: bytes


@dataclass(frozen=True, repr=False)
class Session:
    user_id: int
    login_id: int
    login_date_time: int
    token: str
    user_agent_identity: str


@dataclass(repr=False)
class Scores:
    complete: bool
    rows: list
    reason: str
    pages: int
    next_index: int


class Codec:
    def __init__(self, key, iv, limit=8*1024*1024):
        require(type(key) is bytes and len(key) == 32 and type(iv) is bytes and len(iv) == 16, 'Invalid AES key/IV size')
        require(type(limit) is int and limit > 0, 'Invalid byte limit')
        self._key, self._iv, self.limit = key, iv, limit

    def _encrypt(self, plain):
        padding = 16 - len(plain) % 16
        enc = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).encryptor()
        return enc.update(plain + bytes([padding]) * padding) + enc.finalize()

    def encode(self, plain):
        require(type(plain) is bytes and len(plain) <= self.limit, 'Invalid/oversized plaintext')
        return self._encrypt(zlib.compress(plain, level=6))

    def decode(self, encrypted):
        require(type(encrypted) is bytes and 0 < len(encrypted) <= self.limit + 65536 and len(encrypted) % 16 == 0, 'Invalid ciphertext length')
        dec = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).decryptor()
        plain = dec.update(encrypted) + dec.finalize()
        pad = plain[-1]
        require(1 <= pad <= 16 and plain[-pad:] == bytes([pad])*pad, 'Invalid PKCS7 padding')
        plain = plain[:-pad]
        if len(plain) >= 2 and plain[0] & 15 == 8 and plain[0] >> 4 <= 7 and int.from_bytes(plain[:2], 'big') % 31 == 0:
            try:
                inflater = zlib.decompressobj()
                plain = inflater.decompress(plain, self.limit + 1)
                require(inflater.eof and not inflater.unused_data and not inflater.unconsumed_tail,
                        'Incomplete/oversized/trailing compressed response')
            except zlib.error:
                raise ProtocolError('Invalid zlib response') from None
        require(len(plain) <= self.limit, 'Oversized response')
        return plain


def timestamp(instant):
    require(isinstance(instant, datetime) and instant.utcoffset() is not None, 'Aware datetime required')
    wall = instant.astimezone(timezone(timedelta(hours=8)))
    return f'{wall.year % 100:02d}{wall.month:02d}{wall.day:02d}{wall.hour:02d}{wall.minute:02d}{wall.second:02d}'


class Client:
    def __init__(self, config, transport, clock=lambda: datetime.now(timezone.utc)):
        require(callable(transport) and callable(clock), 'Explicit callback and clock required')
        self.config, self.transport, self.clock = config, transport, clock
        self.codec = Codec(config.aes_key, config.aes_iv)
        self.session = None
        self._login_inputs = None
        self._login_attempted = False

    def _send(self, request):
        try:
            response = self.transport(request)
        except Exception:
            raise ProtocolError('Injected transport failed') from None
        require(isinstance(response, Response) and type(response.body) is bytes, 'Invalid callback response')
        require(type(response.status) is int and response.status == 200, 'Non-200 callback response')
        require(len(response.body) <= self.codec.limit + 65536, 'Oversized callback response')
        return response.body

    def _game_request(self, api, payload, identity):
        c = self.config
        name = hashlib.md5(api.encode('utf-8') + b'MaimaiChn' + c.salt).hexdigest()
        headers = {'User-Agent': name+'#'+identity, 'Mai-Encoding': c.mai_encoding,
                   'Accept': '*/*', 'Charset': 'UTF-8', 'Content-Encoding': 'deflate',
                   'Content-Type': c.game_content_type}
        return Request(api, 'POST', c.game_base+name, headers, self.codec.encode(json_bytes(payload)))

    def _game_response(self, request):
        body = self._send(request)
        require(bool(body), 'Empty HTTP body')
        decoded = self.codec.decode(body)
        require(bool(decoded), 'Empty decoded response')
        return object_json(decoded)

    def _game(self, api, payload, identity):
        return self._game_response(self._game_request(api, payload, identity))

    def login(self, qr, inputs):
        require(not self._login_attempted, 'Client is single-session; create a new client and transport')
        self._login_attempted = True
        require(isinstance(qr, str) and qr.isascii() and len(qr) == 84 and
                qr.startswith('SGWCMAID') and re.fullmatch('[0-9A-F]{64}', qr[20:]), 'Invalid QR shape')
        # Captured QR wall time, then separate numeric epoch sample before QR callback.
        stamp = timestamp(self.clock())
        numeric_instant = self.clock()
        require(isinstance(numeric_instant, datetime) and numeric_instant.utcoffset() is not None, 'Aware numeric clock required')
        epoch = int(numeric_instant.timestamp())
        require(integer(epoch), 'Invalid numeric login time')
        c = self.config
        digest = hashlib.sha256(c.chip_id.encode('ascii') + stamp.encode('ascii') + c.qr_secret).hexdigest().upper()
        payload = {'chipID': c.chip_id, 'openGameID': c.open_game_id, 'key': digest,
                   'qrCode': qr[20:], 'timestamp': stamp}
        lookup = object_json(self._send(Request('QRLookup', 'POST', c.qr_url, dict(c.qr_headers), json_bytes(payload))))
        require(type(lookup.get('errorID')) is int and lookup['errorID'] == 0, 'QR lookup rejected')
        uid = lookup.get('aimeId')
        if not integer(uid):
            uid = lookup.get('userID')
            if isinstance(uid, str) and re.fullmatch('[+-]?[0-9]+', uid) and len(uid) <= 21:
                uid = int(uid)
                require(-(2**63) <= uid < 2**63, 'Invalid identity string range')
        # Local stricter policy than native signed-string fallback.
        require(integer(uid, 1), 'Invalid/nonpositive identity required')
        token = lookup.get('token')
        require(isinstance(token, str) and bool(token.strip()), 'Missing QR token')
        data = {'userId': uid, 'accessCode': '', 'regionId': inputs.region_id,
                'placeId': inputs.place_id, 'clientId': c.chip_id,
                'dateTime': epoch, 'loginDateTime': epoch,
                'isContinue': False, 'genericFlag': 0, 'token': token}
        reply = self._game('UserLoginApi', data, inputs.user_agent_identity)
        require(integer(reply.get('loginId'), 1), 'Invalid loginId; session not committed')
        # Additional local error guard: initial native returnCode validation is not proven.
        if 'returnCode' in reply:
            require(type(reply['returnCode']) is int and reply['returnCode'] == 1, 'Login rejected')
        login_time = reply.get('loginDateTime', epoch)
        require(integer(login_time), 'Invalid loginDateTime')
        self.session = Session(uid, reply['loginId'], login_time, token, inputs.user_agent_identity)
        self._login_inputs = inputs
        return self.session

    def _current(self, session):
        require(session is not None and session is self.session, 'Current successful session required')

    def profile(self, session):
        # HDD GetUserDataApi schema; APK/backend compatibility is unverified.
        self._current(session)
        reply = self._game('GetUserDataApi', {'userId': session.user_id}, session.user_agent_identity)
        if 'returnCode' in reply:
            require(type(reply['returnCode']) is int and reply['returnCode'] == 1, 'Profile returnCode rejected')
        require(integer(reply.get('userId')) and reply['userId'] == session.user_id and
                type(reply.get('userData')) is dict and integer(reply.get('banState')),
                'Invalid profile envelope')
        return reply

    def logout_request(self, session):
        # HDD normal logout, no upload, delayLog, token, or newly sampled time.
        self._current(session)
        inputs = self._login_inputs
        return self._game_request('UserLogoutApi', {
            'userId': session.user_id, 'accessCode': '', 'regionId': inputs.region_id,
            'placeId': inputs.place_id, 'clientId': self.config.chip_id,
            'loginDateTime': session.login_date_time, 'type': 1}, session.user_agent_identity)

    def logout(self, session):
        reply = self._game_response(self.logout_request(session))
        require(type(reply.get('returnCode')) is int and reply['returnCode'] == 1,
                'Logout not confirmed')
        self.session = None
        self._login_inputs = None
        return True

    def scores(self, session, page_budget=512):
        self._current(session)
        require(type(page_budget) is int and page_budget > 0, 'Positive page budget required')
        cursor, rows, seen_cursors, seen_rows, pages = 0, [], set(), set(), 0
        for _ in range(page_budget):
            if cursor in seen_cursors:
                return Scores(False, rows, 'cursor_cycle', pages, cursor)
            seen_cursors.add(cursor)
            try:
                reply = self._game('GetUserMusicApi', {'userId': session.user_id, 'nextIndex': cursor,
                                   'maxCount': 50}, session.user_agent_identity)
                pages += 1
                if 'returnCode' in reply:
                    require(type(reply['returnCode']) is int and reply['returnCode'] == 1, 'Score returnCode rejected')
                next_cursor, groups = reply.get('nextIndex'), reply.get('userMusicList')
                require(integer(next_cursor) and type(groups) is list, 'Invalid score page envelope')
                page, keys = [], set()
                for group in groups:
                    require(type(group) is dict and type(group.get('userMusicDetailList')) is list, 'Invalid score group')
                    for row in group['userMusicDetailList']:
                        require(type(row) is dict and all(integer(row.get(k)) for k in
                                ('musicId', 'level', 'playCount', 'achievement', 'deluxscoreMax')), 'Invalid score row')
                        require(all(integer(row[k]) for k in ('comboStatus', 'syncStatus', 'scoreRank', 'extNum1') if k in row), 'Invalid optional score integer')
                        key = row['musicId'], row['level']
                        require(key not in keys and key not in seen_rows, 'Duplicate score row')
                        keys.add(key)
                        page.append(dict(row))
                rows.extend(page)
                seen_rows.update(keys)
                cursor = next_cursor
                if not groups or cursor == 0:
                    return Scores(True, rows, 'empty_outer_list' if not groups else 'cursor_zero', pages, cursor)
            except ProtocolError as exc:
                return Scores(False, rows, str(exc), pages, cursor)
        return Scores(False, rows, 'page_budget', pages, cursor)

    def run(self, qr, inputs, page_budget=512):
        session = self.login(qr, inputs)
        return session, self.scores(session, page_budget)


def self_test():
    import unittest

    class Tests(unittest.TestCase):
        def setUp(self):
            self.c = Config('https://qr.invalid/lookup', 'https://game.invalid/api/',
                            'A63E01ZZZZZ', 'MAID', b'Q'*32, b'synthetic-salt', bytes(range(32)),
                            bytes(range(16)), 'fixture', 'application/json', {'Content-Type': 'application/json'})
            self.codec = Codec(self.c.aes_key, self.c.aes_iv)
            self.inputs = LoginInputs(1, 2, 'synthetic-identity')
            self.qr = 'SGWCMAID' + 'X'*12 + 'A'*64
            self.calls, self.ticks = [], []

        def clock(self):
            self.ticks.append(1)
            return datetime(2026, 12, 31, 16, tzinfo=timezone.utc) + timedelta(seconds=len(self.ticks)-1)

        def page(self, cursor=0, music=1):
            return {'returnCode': 1, 'nextIndex': cursor, 'userMusicList': [{'userMusicDetailList': [
                {'musicId': music, 'level': 3, 'playCount': 0, 'achievement': 1000000, 'deluxscoreMax': 42}]}]}

        def pipeline(self, pages, login_reply=None, qr_reply=None):
            self.ticks.clear()
            pages = iter(pages)
            def transport(req):
                self.calls.append(req)
                self.assertEqual(req.method, 'POST')
                if req.stage == 'QRLookup':
                    obj = object_json(req.body)
                    self.assertEqual(obj['timestamp'], '270101000000')
                    self.assertEqual(obj['qrCode'], 'A'*64)
                    self.assertEqual(obj['key'], hashlib.sha256(b'A63E01ZZZZZ270101000000'+b'Q'*32).hexdigest().upper())
                    return Response(200, json_bytes(qr_reply if qr_reply is not None else {'errorID': 0, 'aimeId': 123, 'token': 'fixture'}))
                obj = object_json(self.codec.decode(req.body))
                expected = hashlib.md5(req.stage.encode()+b'MaimaiChnsynthetic-salt').hexdigest()
                self.assertEqual(req.url, self.c.game_base+expected)
                self.assertEqual(req.headers['User-Agent'], expected+'#synthetic-identity')
                self.assertEqual(req.headers['Content-Encoding'], 'deflate')
                if req.stage == 'UserLoginApi':
                    self.assertEqual(obj, {'userId': 123, 'accessCode': '', 'regionId': 1, 'placeId': 2,
                                          'clientId': self.c.chip_id, 'dateTime': 1798732801, 'loginDateTime': 1798732801,
                                          'isContinue': False, 'genericFlag': 0, 'token': 'fixture'})
                    reply = login_reply if login_reply is not None else {'loginId': 9}
                else:
                    self.assertEqual(set(obj), {'userId', 'nextIndex', 'maxCount'})
                    self.assertEqual(obj['maxCount'], 50)
                    reply = next(pages)
                    if isinstance(reply, Exception):
                        raise reply
                return Response(200, self.codec.encode(json_bytes(reply)))
            return Client(self.c, transport, self.clock)

        def test_codec(self):
            for raw in (b'', b'{}', b'A'*16, b'A'*10000, bytes(range(256))):
                self.assertEqual(self.codec.decode(self.codec.encode(raw)), raw)
                self.assertEqual(self.codec.decode(self.codec._encrypt(raw)), raw)
            for level in (0, 1, 6, 9):
                other = zlib.compress(b'{}', level)
                self.assertEqual(self.codec.decode(self.codec._encrypt(other)), b'{}')
            with self.assertRaises(ProtocolError): self.codec.decode(b'bad')
            enc = Cipher(algorithms.AES(self.c.aes_key), modes.CBC(self.c.aes_iv)).encryptor()
            with self.assertRaises(ProtocolError): self.codec.decode(enc.update(b'x'*15+b'\x00')+enc.finalize())
            with self.assertRaises(ProtocolError): self.codec.decode(self.codec._encrypt(b'\x78\x9cbad'))
            with self.assertRaises(ProtocolError): Codec(b'x', b'y')
            with self.assertRaises(ProtocolError): Codec(self.c.aes_key, self.c.aes_iv, 20).decode(self.codec.encode(b'A'*100))
            with self.assertRaises(ProtocolError): self.codec.decode(self.codec._encrypt(zlib.compress(b'{}')+b'junk'))

        def test_aes_known_answer(self):
            # NIST SP 800-38A F.2.5 first block, public test vector, no target constants.
            key = bytes.fromhex('603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4')
            raw = bytes.fromhex('6bc1bee22e409f96e93d7e117393172a')
            self.assertEqual(Codec(key, bytes(range(16)))._encrypt(raw)[:16].hex(), 'f58c4c04d6e5f1ba779eabfb5f7bfbd6')

        def test_pipeline(self):
            client = self.pipeline([self.page(7), self.page(0, 2)])
            session, result = client.run(self.qr, self.inputs)
            self.assertTrue(result.complete)
            self.assertEqual((len(result.rows), session.login_date_time, session.token), (2, 1798732801, 'fixture'))
            self.assertEqual(len(self.ticks), 2)
            self.assertEqual([object_json(self.codec.decode(r.body))['nextIndex'] for r in self.calls[2:]], [0, 7])
            self.assertEqual(result.rows[0]['playCount'], 0)
            self.assertNotIn('A63E01', repr(self.c))
            self.assertNotIn('synthetic', repr(self.calls[0]))

        def test_partial_and_terminal(self):
            cases = [([self.page(7), self.page(7, 2)], 512, False, 'cursor_cycle', 2),
                     ([self.page(7), self.page(0)], 512, False, 'Duplicate score row', 1),
                     ([self.page(7)], 1, False, 'page_budget', 1),
                     ([self.page(7), RuntimeError('private data')], 512, False, 'Injected transport failed', 1),
                     ([{'nextIndex': 9, 'userMusicList': []}], 512, True, 'empty_outer_list', 0),
                     ([{'nextIndex': 9, 'userMusicList': [{'userMusicDetailList': []}]}, self.page()], 512, True, 'cursor_zero', 1),
                     ([{'returnCode': 0}], 512, False, 'Score returnCode rejected', 0),
                     ([{'nextIndex': 0}], 512, False, 'Invalid score page envelope', 0)]
            for pages, budget, complete, reason, count in cases:
                with self.subTest(reason=reason):
                    _, result = self.pipeline(pages).run(self.qr, self.inputs, budget)
                    self.assertEqual((result.complete, result.reason, len(result.rows)), (complete, reason, count))

        def test_failures(self):
            for reply in ({'loginId': -1}, {'loginId': True}, {'loginId': 1, 'returnCode': 0}):
                with self.assertRaises(ProtocolError): self.pipeline([], login_reply=reply).run(self.qr, self.inputs)
            for reply in ({'errorID': 1}, {'errorID': 0, 'aimeId': True, 'token': ''}, {'errorID': 0, 'userID': '-1', 'token': ''}):
                with self.assertRaises(ProtocolError): self.pipeline([], qr_reply=reply).run(self.qr, self.inputs)
            for raw in (b'{"a":1,"a":2}', b'[]', b'{"a":NaN}', b'\xff'):
                with self.assertRaises(ProtocolError): object_json(raw)
            with self.assertRaises(ProtocolError): timestamp(datetime(2026, 1, 1))
            with self.assertRaises(ProtocolError): self.pipeline([]).login(self.qr.lower(), self.inputs)
            client = Client(self.c, lambda _: Response(503, b'private'), self.clock)
            with self.assertRaises(ProtocolError): client.login(self.qr, self.inputs)

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    if sys.argv[1:] == ['--self-test']:
        raise SystemExit(self_test())
    print('Offline library only. Run: python3 maimai-minimal-client.py --self-test')
    raise SystemExit(0 if len(sys.argv) == 1 else 2)
