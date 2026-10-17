"""Exercise public offerings through CLI and real codec/transport, no sockets."""
import contextlib
import io
import json
import sys
import unittest
from unittest.mock import patch
from test_ticket_cli import TicketCLITests, live, m


class GameChargeCLI(unittest.TestCase):
    def test_offerings_and_malformed_reply(self):
        fixture = TicketCLITests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        row = dict(orderId=1, chargeId=2, price=1,
                   startDate='2026-01-01 00:00:00', endDate='2027-01-01 00:00:00')
        reply = dict(length=1, gameChargeList=[row])
        calls = []
        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                assert request.get_header('User-agent').endswith('#0')
                assert m.object_json(fixture.codec.decode(request.data)) == {'isAll': False}
                body = fixture.codec.encode(m.json_bytes(reply))
                from email.message import Message
                class Response:
                    status = 200
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def info(self): return Message()
                    def read(self, limit): return body[:limit]
                return Response()
        def run():
            output = io.StringIO()
            with patch.object(live, 'load_config', return_value=(fixture.config, {})), patch.object(live.urllib.request, 'build_opener', return_value=Opener()), patch.object(sys, 'argv', ['client', '--game-charge']), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                code = live.main()
            return code, json.loads(output.getvalue())
        code, result = run()
        self.assertEqual(code, 0)
        self.assertEqual(result['target_listed'], {'2': True, '3': False})
        self.assertEqual(len(calls), 1)
        fixture.unchanged()
        reply['length'] = 2
        code, result = run()
        self.assertEqual(code, 5)
        self.assertEqual(result['status'], 'indeterminate')
        fixture.unchanged()
