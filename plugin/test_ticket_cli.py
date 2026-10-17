"""Synthetic CLI -> real Transport -> inventory parser; no sockets or production data."""
import contextlib
import dataclasses
from email.message import Message
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import qr_sync as q

spec = importlib.util.spec_from_file_location('ticket_live_test', Path(__file__).resolve().parent.parent / 'maimai-live-client.py')
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)
m = live.m
USER = '12345678'

class TicketCLITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.private = Path(tmp.name); self.private.chmod(0o700)
        for p in (patch.object(live, 'PRIVATE', self.private),
                  patch('socket.socket', side_effect=AssertionError('offline sockets forbidden'))):
            p.start(); self.addCleanup(p.stop)
        self.config = m.Config('https://qr.invalid/lookup', 'https://game.invalid/api/',
            'A63E01ABCDE', 'TEST', b'Q'*32, b'fixture', bytes(range(32)), bytes(range(16)),
            'test', 'application/json', {'Content-Type':'application/json'})
        self.codec = m.Codec(self.config.aes_key, self.config.aes_iv)
        self.rows = [{'chargeId':2, 'stock':1, 'purchaseDate':'2026-10-14 04:00:00', 'validDate':'2027-01-12 04:00:00'}]
        self.reply = {'userId':123, 'length':1, 'userChargeList':self.rows}
        self.requests = []
        self.error = None
        session = m.Session(user_id=123, login_id=456, login_date_time=1000, token='synthetic-token', user_agent_identity='123')
        inputs = m.LoginInputs(1, 2, '123')
        live.write_session(USER, {'state':'active', 'session':dataclasses.asdict(session),
            'inputs':dataclasses.asdict(inputs), 'cookies':[],
            'game_base':self.config.game_base, 'chip_id':self.config.chip_id,
            'login_success_epoch':1000, 'auto_logout_attempt_epoch':1840}, reserve=True)
        self.path = live.session_path(USER)
        self.before = self.path.read_bytes()
        self.before_mtime = self.path.stat().st_mtime_ns

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error: raise self.error
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(m.object_json(self.codec.decode(request.data)), {'userId':123})
        body = self.codec.encode(m.json_bytes(self.reply))
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def info(self): return Message()
            def read(self, limit): return body[:limit]
        return Response()

    def cli(self, owner=USER):
        output, diagnostics = io.StringIO(), io.StringIO()
        opener = Mock(); opener.open.side_effect = self.open
        with patch.object(live, 'load_config', return_value=(self.config, {})), \
             patch.object(live.urllib.request, 'build_opener', return_value=opener), \
             patch.object(sys, 'argv', ['client', '--ticket-inventory', '--session-owner', owner]), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostics):
            code = live.main()
        return code, output.getvalue(), diagnostics.getvalue()

    def unchanged(self):
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.path.stat().st_mtime_ns, self.before_mtime)

    def test_real_transport_emits_one_json_document(self):
        code, output, diagnostics = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {'status':'ok', 'tickets':self.rows})
        self.assertEqual(len(self.requests), 1)
        self.assertIn('GetUserChargeApi', diagnostics)
        self.assertNotIn('synthetic-token', output + diagnostics)
        self.unchanged()

    def test_parent_parser_and_inherited_lock(self):
        def run(argv, **kwargs):
            self.assertIn('--ticket-inventory', argv)
            self.assertEqual(kwargs['pass_fds'], (q._session_local.fd,))
            self.assertEqual(kwargs['env']['MAIMAI_SESSION_LOCK_FD'], str(q._session_local.fd))
            with patch.dict(os.environ, {'MAIMAI_SESSION_LOCK_FD':str(q._session_local.fd)}):
                code, output, _ = self.cli()
            return subprocess.CompletedProcess(argv, code, output.encode(), b'')
        with q.session_lock(self.private), patch.object(q.subprocess, 'run', side_effect=run):
            self.assertEqual(q.inventory(USER, private=self.private), self.rows)
        self.assertEqual(len(self.requests), 1)
        self.unchanged()

    def test_empty_inventory_remains_observation(self):
        self.reply.update(length=0, userChargeList=[])
        code, output, _ = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['tickets'], [])
        self.unchanged()

    def test_network_failure_is_indeterminate_no_retry(self):
        self.error = TimeoutError('synthetic-secret')
        code, output, diagnostics = self.cli()
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(output)['status'], 'indeterminate')
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn('synthetic-secret', output + diagnostics)
        self.unchanged()

    def test_bad_response_identity_keeps_session(self):
        self.reply['userId'] = 999
        code, output, _ = self.cli()
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)['reason'], 'inventory_protocol_error')
        self.assertEqual(len(self.requests), 1)
        self.unchanged()

    def test_missing_owner_sends_nothing(self):
        code, output, _ = self.cli('99999999')
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(output)['status'], 'indeterminate')
        self.assertEqual(self.requests, [])
        self.unchanged()

if __name__ == '__main__': unittest.main()
