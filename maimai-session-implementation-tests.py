#!/usr/bin/env python3
"""Socket-free synthetic session/CLI regressions. Never loads production config."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from email.message import Message

spec = importlib.util.spec_from_file_location('session_live_fixture', Path(__file__).with_name('maimai-live-client.py'))
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)
m = live.m

class Tests(unittest.TestCase):
    def setUp(self):
        self.c = m.Config('https://game.invalid/qr', 'https://game.invalid/api/',
                          'A63E01ZZZZZ', 'GAMEID', b'Q'*32, b'fixture', bytes(range(32)),
                          bytes(range(16)), 'fixture', 'application/json', {})
        self.codec = m.Codec(self.c.aes_key, self.c.aes_iv)
        self.qr = 'SGWCMAID'+'X'*12+'A'*64
        self.inputs = m.LoginInputs(1, 2, 'fixture')
        self.calls = []

    def client(self, replies):
        def send(req):
            self.calls.append(req)
            if req.stage == 'QRLookup':
                return m.Response(200, m.json_bytes({'errorID':0,'aimeId':123,'token':'fixture'}))
            reply = replies[req.stage]
            if isinstance(reply, Exception): raise reply
            return m.Response(200, reply if isinstance(reply, bytes) else self.codec.encode(m.json_bytes(reply)))
        return m.Client(self.c, send)

    def test_saved_logout_and_ownership(self):
        client = self.client({'UserLoginApi':{'loginId':9,'loginDateTime':42}, 'UserLogoutApi':{'returnCode':1}})
        session = client.login(self.qr, self.inputs)
        req = client.logout_request(session)
        self.assertEqual(m.object_json(self.codec.decode(req.body)), {'userId':123,'accessCode':'','regionId':1,'placeId':2,'clientId':self.c.chip_id,'loginDateTime':42,'type':1})
        with self.assertRaises(m.ProtocolError): client.logout_request(m.Session(123,9,42,'fixture','fixture'))
        client.logout(session)
        with self.assertRaises(m.ProtocolError): client.logout(session)
        with self.assertRaises(m.ProtocolError): client.login(self.qr, self.inputs)

    def test_unknown_logout_and_profile_errors(self):
        replies = {'UserLoginApi':{'loginId':9}, 'UserLogoutApi':{}, 'GetUserDataApi':{'returnCode':100}, 'GetUserMusicApi':{'returnCode':100}}
        client = self.client(replies)
        session = client.login(self.qr, self.inputs)
        with self.assertRaisesRegex(m.ProtocolError, 'Profile returnCode rejected'): client.profile(session)
        self.assertFalse(client.scores(session).complete)
        with self.assertRaisesRegex(m.ProtocolError, 'Logout not confirmed'): client.logout(session)
        self.assertIs(client.session, session)
        replies['GetUserDataApi'] = {'userId':123,'userData':{'private':'discard'},'banState':0}
        self.assertIsInstance(client.profile(session)['userData'], dict)

    def test_empty_boundaries(self):
        for body, reason in [(b'', 'Empty HTTP body'), (self.codec.encode(b''), 'Empty decoded response'), (b'bad', 'Invalid ciphertext length')]:
            client = self.client({'UserLoginApi':{'loginId':9},'GetUserMusicApi':body})
            session = client.login(self.qr,self.inputs)
            scores = client.scores(session)
            self.assertFalse(scores.complete)
            self.assertEqual(scores.reason, reason)

    def test_cookies_and_transport_decode_boundary(self):
        transport = live.Transport(self.c)
        transport.identity = '123'
        requests = []
        class Response:
            status = 200
            headers = Message()
            headers.add_header('Set-Cookie','fixture=synthetic; Path=/; Secure')
            headers.add_header('Set-Cookie','wide=synthetic; Domain=game.invalid; Path=/; Secure')
            def info(self): return self.headers
            def read(self, limit): return b'bad'
            def __enter__(self): return self
            def __exit__(self,*args): pass
        class Opener:
            def open(self, req, timeout):
                requests.append(req)
                return Response()
        transport.opener = Opener()
        def req(stage): return m.Request(stage,'POST',self.c.game_base+'0'*32,{'User-Agent':'fixture'},b'fixture')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            transport(req('UserLoginApi'))
            self.assertIsNone(requests[0].get_header('Cookie'))
            transport(req('GetUserMusicApi'))
        self.assertEqual(requests[1].get_header('Cookie'), 'fixture=synthetic')
        self.assertIsNone(transport.last_error)  # invalid AES is not a network failure
        self.assertNotIn('prefix',out.getvalue())
        for url in [self.c.qr_url,'https://other.invalid/api/','https://sub.game.invalid/api/','https://game.invalid:444/api/','http://game.invalid/api/']:
            r = live.urllib.request.Request(url)
            transport.cookies.add_cookie_header(r)
            self.assertIsNone(r.get_header('Cookie'))
        for url in [self.c.qr_url, 'https://other.invalid/api/']:
            jar = live.http.cookiejar.CookieJar(policy=live.GameCookiePolicy(self.c))
            jar.extract_cookies(Response(), live.urllib.request.Request(url))
            self.assertEqual(len(jar), 0)
        other = live.Transport(self.c)
        self.assertEqual(len(other.cookies),0)
        transport.clear_session()
        self.assertEqual(len(transport.cookies),0)
        with self.assertRaises(live.SafeFailure): transport(req('UserLogoutApi'))

    def test_cli_login_failure_and_opt_in(self):
        for flags, login, profile, logout, expected in [
            (['--logout'], {'loginId':9,'returnCode':100}, {}, {}, ['QRLookup','UserLoginApi']),
            ([], {'loginId':9}, {}, {}, ['QRLookup','UserLoginApi','GetUserMusicApi']),
            (['--preload-profile','--logout'], {'loginId':9}, {'returnCode':100}, {'returnCode':1}, ['QRLookup','UserLoginApi','GetUserDataApi','UserLogoutApi']),
            (['--logout'], {'loginId':9}, {}, {}, ['QRLookup','UserLoginApi','GetUserMusicApi','UserLogoutApi']),
        ]:
            self.calls.clear()
            fixture = self.client({'UserLoginApi':login,'GetUserDataApi':profile,'UserLogoutApi':logout,'GetUserMusicApi':{'nextIndex':0,'userMusicList':[]}})
            class FakeTransport:
                last_error = None
                calls = 0
                stage = 'none'
                closed = False
                def __call__(self, req):
                    self.calls += 1
                    self.stage = req.stage
                    return fixture.transport(req)
                def clear_session(self): self.closed = True
            t = FakeTransport()
            with tempfile.TemporaryDirectory() as td:
                result = Path(td)/'result.json'
                with patch.object(live,'PRIVATE',Path(td)), patch.object(live,'load_config',return_value=(self.c,{'region_id':1,'place_id':2})), patch.object(live,'Transport',return_value=t), patch.object(sys,'argv',['fixture','--live','--qr-stdin','--result',str(result)]+flags), patch.object(sys,'stdin',io.TextIOWrapper(io.BytesIO(self.qr.encode()))), patch.object(live.signal,'signal'), patch.object(live.signal,'alarm'), contextlib.redirect_stdout(io.StringIO()):
                    code = live.main()
                data = json.loads(result.read_text())
                self.assertEqual(result.stat().st_mode & 0o777,0o600)
                self.assertEqual([r.stage for r in self.calls],expected)
                self.assertNotIn('userData',result.read_text())
                if '--logout' in flags and login.get('returnCode') != 100:
                    self.assertEqual(t.closed,logout.get('returnCode') == 1)
                    if not t.closed:
                        self.assertEqual(data['summary']['logout'],'unconfirmed')
                        self.assertNotEqual(code,0)

if __name__ == '__main__':
    with patch.object(socket, 'socket', side_effect=AssertionError('Network forbidden')):
        unittest.main(verbosity=2)
