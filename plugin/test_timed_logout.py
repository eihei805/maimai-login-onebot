"""All timing/concurrency fixtures are synthetic, sockets forbidden."""
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import qr_sync as q
import timed_logout as t

spec = importlib.util.spec_from_file_location('timed_live', Path(__file__).resolve().parent.parent/'maimai-live-client.py')
live = importlib.util.module_from_spec(spec); spec.loader.exec_module(live)
USER='12345678'


def lock_process(private, started, entered):
    # fork inherits the parent's open description; close that copy first.
    inherited = getattr(q._session_local, 'fd', None)
    if inherited is not None:
        os.close(inherited)
        q._session_local.fd = None
    started.set()
    with q.session_lock(Path(private)):
        entered.set()


class TimingTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.private=Path(tmp.name); os.chmod(self.private,0o700)
        p=patch.object(live,'PRIVATE',self.private);p.start();self.addCleanup(p.stop)
        p=patch('socket.socket',side_effect=AssertionError('offline'));p.start();self.addCleanup(p.stop)
        self.path=q.session_file(USER,self.private)
    def record(self, **fields):
        data=dict(owner=USER,version=1,state='active',login_success_epoch=1000,**fields)
        t.write_record(self.path,data); return data
    def test_manual_59_60_and_cli_gate_before_restore(self):
        self.record(); sent=[]; runner=Mock(return_value=True)
        sync=q.QRSync(self.private,logout_runner=runner)
        for now,calls in ((1059,0),(1060,1)):
            sync.gate.acquire()
            with patch.object(q.time,'time',return_value=now):
                sync._logout_work(USER,lambda uid,text:sent.append(text))
            self.assertEqual(runner.call_count,calls)
        self.assertIn('1 秒',sent[0]);self.assertIn('尚未核验',sent[1])
        with patch.object(live.time,'time',return_value=1059),patch.object(live,'restore_session') as restore:
            with self.assertRaisesRegex(live.SafeFailure,'logout_wait_1'):
                live.logout_owned(USER,Mock(),Mock())
            restore.assert_not_called()
        with patch.object(live.time,'time',return_value=1060),patch.object(live,'restore_session',side_effect=RuntimeError('boundary')) as restore:
            with self.assertRaisesRegex(RuntimeError,'boundary'):live.logout_owned(USER,Mock(),Mock())
            restore.assert_called_once()
    def test_839_840_restart_once_failure_retained_manual_retry(self):
        self.record(); runner=Mock(side_effect=RuntimeError('synthetic')); sent=[]
        self.assertEqual(t.run_once(private=self.private,now=1839,logout_runner=runner),0)
        self.assertEqual(t.run_once(private=self.private,now=1840,logout_runner=runner,send=lambda uid,text:sent.append(text)),1)
        self.assertTrue(self.path.exists()); self.assertEqual(q.session_record(USER,self.private)['auto_logout_attempt_epoch'],1840)
        self.assertIn('记录保留',sent[0]); self.assertIn('/logout',sent[0])
        # A fresh entry discovers persistent marker, independent of memory.
        self.assertEqual(t.run_once(private=self.private,now=2000,logout_runner=runner),0)
        runner.assert_called_once()
        retry=Mock(return_value=True);sync=q.QRSync(self.private,logout_runner=retry);sync.gate.acquire()
        with patch.object(q.time,'time',return_value=2000):sync._logout_work(USER,lambda *args:None)
        retry.assert_called_once()
    def test_marker_written_before_call_and_acceptance_message(self):
        self.record(); sent=[]
        def close(user,**kw):
            self.assertEqual(q.session_record(user,self.private)['auto_logout_attempt_epoch'],1840)
            self.path.unlink();return True
        self.assertEqual(t.run_once(private=self.private,now=1840,logout_runner=close,send=lambda uid,text:sent.append(text)),1)
        self.assertIn('接口已接受',sent[0]);self.assertIn('尚未核验',sent[0])
    def test_no_legacy_unknown_closed_bad_auto(self):
        runner=Mock()
        for data in ({'state':'active'}, {'state':'unknown','login_success_epoch':1000}, {'state':'closed','login_success_epoch':1000}, {'state':'active','login_success_epoch':True}, {'state':'active','login_success_epoch':-1}):
            t.write_record(self.path,dict(owner=USER,version=1,**data))
            self.assertEqual(t.run_once(private=self.private,now=99999,logout_runner=runner),0)
        runner.assert_not_called()
    def test_persist_keeps_deadline_and_attempt(self):
        original=self.record(auto_logout_attempt_epoch=1840)
        import dataclasses
        @dataclasses.dataclass
        class Fixture: value:int=1
        client=Mock(session=Fixture(),_login_inputs=Fixture())
        client.config.game_base='https://game.invalid';client.config.chip_id='fixture'
        live.persist_session(USER,client,Mock(cookies=[]),login_success_epoch=9999)
        data=live.read_session(USER)
        for key in ('login_success_epoch','auto_logout_attempt_epoch'):self.assertEqual(data[key],original[key])
    def test_crossprocess_lock_serializes_capture_manual_timer(self):
        ctx=multiprocessing.get_context('fork'); started=ctx.Event();entered=ctx.Event()
        with q.session_lock(self.private):
            proc=ctx.Process(target=lock_process,args=(str(self.private),started,entered));proc.start()
            self.assertTrue(started.wait(2));self.assertFalse(entered.wait(.1))
        proc.join(2);self.assertEqual(proc.exitcode,0);self.assertTrue(entered.is_set())
    def test_manual_auto_race_exactly_one_successful_dispatch(self):
        self.record();calls=[]; sent=[]
        def close(user,**kw): calls.append(user);self.path.unlink();return True
        sync=q.QRSync(self.private,logout_runner=close);sync.gate.acquire()
        with patch.object(q.time,'time',return_value=1840):
            manual=threading.Thread(target=sync._logout_work,args=(USER,lambda *args:None))
            auto=threading.Thread(target=lambda:t.run_once(private=self.private,now=1840,logout_runner=close,send=lambda *args:None))
            manual.start();auto.start();manual.join(2);auto.join(2)
        self.assertFalse(manual.is_alive());self.assertFalse(auto.is_alive());self.assertEqual(calls,[USER])

if __name__=='__main__':unittest.main()
