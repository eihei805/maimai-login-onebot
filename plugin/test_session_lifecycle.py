"""Offline retained session/logout tests: synthetic config, cookies and wire only."""
import dataclasses
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import qr_sync as q

spec = importlib.util.spec_from_file_location('live_session_test', Path(__file__).resolve().parent.parent/'maimai-live-client.py')
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)
m = live.m
USER = '12345678'
QR = 'SGWCMAID'+'X'*12+'A'*64

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.private = Path(tmp.name); os.chmod(self.private, 0o700)
        p = patch.object(live, 'PRIVATE', self.private); p.start(); self.addCleanup(p.stop)
        p = patch('socket.socket', side_effect=AssertionError('offline')); p.start(); self.addCleanup(p.stop)
        self.config = m.Config('https://qr.invalid/lookup', 'https://game.invalid/api/',
            'A63E01ABCDE','GAMEID',b'Q'*32,b'fixture',bytes(range(32)),bytes(range(16)),
            'test','application/json',{'Content-Type':'application/json'})
        self.codec = m.Codec(self.config.aes_key,self.config.aes_iv)
        self.calls = []
        self.reject = False
    def fixture(self, req):
        self.calls.append(req.stage)
        if req.stage == 'QRLookup':
            return m.Response(200,m.json_bytes({'errorID':0,'aimeId':123,'token':'private-fixture-token'}))
        if req.stage == 'UserLoginApi': reply = {'returnCode':1,'loginId':456}
        elif req.stage == 'UserLogoutApi':
            payload = m.object_json(self.codec.decode(req.body))
            self.assertEqual(payload['userId'],123)
            self.assertEqual(payload['loginDateTime'],self.original.login_date_time)
            self.assertNotIn('token',payload)
            reply = {'returnCode':0 if self.reject else 1}
        else: reply = {'nextIndex':0,'userMusicList':[]}
        return m.Response(200,self.codec.encode(m.json_bytes(reply)))
    def test_rejected_lookup_never_starts_game_login(self):
        for lookup in ({'errorID': 1}, {'errorID': 0, 'aimeId': 0, 'token': 'fixture'},
                       {'errorID': 0, 'aimeId': 123, 'token': ''},
                       {'errorID': 0, 'aimeId': 123, 'token': '   '}):
            calls = []
            def send(req):
                calls.append(req.stage)
                return m.Response(200, m.json_bytes(lookup))
            client = m.Client(self.config, send)
            with self.assertRaises(m.ProtocolError):
                client.login(QR, m.LoginInputs(1, 2, ''))
            self.assertEqual(calls, ['QRLookup'])
            self.assertIsNone(client.session)

    def committed(self):
        transport = live.Transport(self.config)
        client = m.Client(self.config,self.fixture)
        self.original = client.login(QR,m.LoginInputs(1,2,''))
        transport.identity = '123'
        cookie = http.cookiejar.Cookie(0,'sid','cookie-secret',None,False,'game.invalid',False,
            False,'/',True,True,None,True,None,None,{},False)
        transport.cookies.set_cookie(cookie)
        live.write_session(USER,{'state':'unknown'},reserve=True)
        live.persist_session(USER,client,transport)
        return client,transport
    def test_persist_permissions_ownership_and_no_qr(self):
        self.committed(); path = live.session_path(USER)
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertNotIn(QR,path.read_text())
        record = live.read_session(USER); self.assertEqual(record['state'],'active')
        with self.assertRaises(FileExistsError): live.write_session(USER,{'state':'unknown'},reserve=True)
        other = '22345678'; live.session_path(other).write_bytes(path.read_bytes()); os.chmod(live.session_path(other),0o600)
        with self.assertRaises(live.SafeFailure): live.read_session(other)
        with self.assertRaises(live.SafeFailure): live.session_path('../12345')
        os.chmod(path,0o644)
        with self.assertRaises(live.SafeFailure): live.read_session(USER)
    def test_restore_real_logout_success_and_cookies(self):
        self.committed(); transport = live.Transport(self.config)
        client = m.Client(self.config,self.fixture)
        restored = live.restore_session(USER,client,transport)
        self.assertEqual(dataclasses.asdict(restored),dataclasses.asdict(self.original))
        self.assertEqual(transport.identity,'123')
        self.assertEqual(list(transport.cookies)[0].value,'cookie-secret')
        self.calls.clear()
        live.logout_owned(USER,client,transport)
        self.assertEqual(self.calls,['UserLogoutApi'])
        self.assertFalse(live.session_path(USER).exists())
        self.assertTrue(transport._closed)
    def test_failed_logout_retains_session_unknown_and_symlink_reject(self):
        self.committed(); before = live.session_path(USER).read_bytes(); self.reject=True
        with self.assertRaises(m.ProtocolError): live.logout_owned(USER,m.Client(self.config,self.fixture),live.Transport(self.config))
        self.assertEqual(live.session_path(USER).read_bytes(),before)
        live.write_session(USER,{'state':'unknown'})
        with self.assertRaises(live.SafeFailure): live.restore_session(USER,m.Client(self.config,self.fixture),live.Transport(self.config))
        target = self.private/'target'; target.write_bytes(before); os.chmod(target,0o600)
        live.session_path(USER).unlink(); live.session_path(USER).symlink_to(target)
        with self.assertRaises(OSError): live.read_session(USER)
        self.assertTrue(q.session_exists(USER,self.private))
    def test_confirmed_closed_marker_cleanup_without_replay(self):
        live.write_session(USER,{'state':'closed'},reserve=True)
        live.logout_owned(USER,m.Client(self.config,self.fixture),live.Transport(self.config))
        self.assertFalse(live.session_path(USER).exists())
        self.assertEqual(self.calls,[])
    def event(self,text,typ='private'):
        return {'post_type':'message','message_type':typ,'user_id':int(USER),'raw_message':text,
                'message':[{'type':'text','data':{'text':text}}]}
    def test_login_callback_duplicate_group_and_malformed(self):
        sent=[]; callback=Mock()
        def runner(user,qr,**kw):
            self.assertTrue(kw['retain']); self.committed()
            return q.CaptureResult(USER,self.private/'result',[],{'status':'complete','rows':0,'logout':'unconfirmed','session':'active'})
        sync=q.QRSync(self.private,runner=Mock(side_effect=runner),on_result=callback)
        send=lambda uid,text: sent.append(text)
        sync._consent(USER,True)
        sync.handle(self.event('/login '+QR),send)
        self.assertTrue(sync.gate.acquire(timeout=2)); sync.gate.release()
        callback.assert_called_once(); self.assertIn('会话已保留',sent[-1])
        sync.handle(self.event('/login '+QR),send); self.assertEqual(sync.runner.call_count,1)
        self.assertIn('先 /logout',sent[-1])
        for text in ('/login bad','/logout bad','/LOGIN secret'):
            sync.handle(self.event(text),send); self.assertTrue(q.group_sensitive(self.event(text,'group')))
        self.assertNotIn(QR,repr(sent)); self.assertNotIn('secret',repr(sent))
    def test_logout_runner_stdin_none_and_failure_retention(self):
        self.committed()
        def run(argv,**kw):
            self.assertEqual(argv[-3:],['--logout-session','--session-owner',USER])
            self.assertEqual(kw['stdin'],subprocess.DEVNULL); self.assertNotIn('input',kw)
            self.assertNotIn('--live',argv); self.assertNotIn('--qr-stdin',argv)
            return Mock(returncode=3)
        with patch.object(q.subprocess,'run',side_effect=run):
            with self.assertRaises(q.CaptureError): q.logout(USER,private=self.private)
        self.assertTrue(live.session_path(USER).exists())
        def success(*a,**kw): live.session_path(USER).unlink(); return Mock(returncode=0)
        with patch.object(q.subprocess,'run',side_effect=success): self.assertTrue(q.logout(USER,private=self.private))
    def test_logout_handle_gate_no_session_and_failure(self):
        sent=[]; send=lambda uid,text: sent.append(text)
        closer=Mock(side_effect=RuntimeError('token-secret'))
        sync=q.QRSync(self.private,logout_runner=closer)
        sync.handle(self.event('/logout'),send)
        self.assertTrue(sync.gate.acquire(timeout=2)); sync.gate.release()
        closer.assert_not_called(); self.assertIn('未执行',sent[-1])
        self.committed()
        sync.handle(self.event('/logout'),send)
        self.assertTrue(sync.gate.acquire(timeout=2)); sync.gate.release()
        self.assertIn('登出未确认',sent[-1]); self.assertTrue(live.session_path(USER).exists())
        self.assertNotIn('token-secret',repr(sent))
        self.assertTrue(sync.gate.acquire(blocking=False))
        try: sync.handle(self.event('/logout'),send); self.assertIn('正在进行',sent[-1])
        finally: sync.gate.release()
    def test_capture_retained_stdin_flags_and_timeout_unknown(self):
        def run(argv,**kw):
            self.assertEqual(kw['input'],QR.encode()); self.assertNotIn(QR,repr(argv))
            self.assertIn('--session-owner',argv); self.assertNotIn('--logout',argv)
            self.committed()
            path=Path(argv[-1]); fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f: json.dump({'summary':{'status':'complete','rows':0,'session':'active'},'scores':[]},f)
            return Mock(returncode=0)
        with patch.object(q.subprocess,'run',side_effect=run):
            result=q.capture(USER,QR,private=self.private,retain=True)
        self.assertEqual(result.summary['session'],'active')
        live.session_path(USER).unlink()
        def timeout(*a,**kw):
            live.write_session(USER,{'state':'unknown'},reserve=True)
            raise subprocess.TimeoutExpired(['safe'],210)
        with patch.object(q.subprocess,'run',side_effect=timeout):
            with self.assertRaises(q.CaptureError): q.capture(USER,QR,private=self.private,retain=True)
        self.assertEqual(live.read_session(USER)['state'],'unknown')

if __name__ == '__main__': unittest.main()
