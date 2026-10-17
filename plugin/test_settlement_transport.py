"""Real adapter write gate with synthetic requests; no sockets."""
import unittest
from unittest.mock import Mock, patch
from test_ticket_cli import live, m

class SettlementTransportTests(unittest.TestCase):
    def setUp(self):
        self.config = m.Config('https://qr.invalid/lookup', 'https://game.invalid/api/',
            'A63E01ABCDE', 'TEST', b'Q'*32, b'fixture', bytes(range(32)), bytes(range(16)),
            'test', 'application/json', {'Content-Type':'application/json'})
        self.req = m.Request('UpsertUserAllApi', 'POST', self.config.game_base+'a'*32,
                             {'User-Agent':'test#123'}, b'synthetic')

    def test_default_transport_cannot_write(self):
        t=live.Transport(self.config); t.identity='123'
        with patch.object(t.opener, 'open') as send:
            with self.assertRaises(live.SafeFailure): t(self.req)
            send.assert_not_called()

    def test_timeout_consumes_transport_allowance(self):
        t=live.Transport(self.config, allow_settlement=True); t.identity='123'
        with patch.object(t.opener, 'open', side_effect=TimeoutError('private-detail')) as send:
            with self.assertRaisesRegex(live.SafeFailure, 'network_or_tls_failure'): t(self.req)
            with self.assertRaisesRegex(live.SafeFailure, 'already_attempted'): t(self.req)
            self.assertEqual(send.call_count, 1)

    def test_success_cannot_be_repeated(self):
        t=live.Transport(self.config, allow_settlement=True); t.identity='123'
        response=Mock(); response.status=200; response.read.return_value=b'ack'
        response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
        with patch.object(t.opener, 'open', return_value=response) as send, patch.object(t.cookies, 'extract_cookies'):
            self.assertEqual(t(self.req).body,b'ack')
            with self.assertRaises(live.SafeFailure): t(self.req)
            self.assertEqual(send.call_count,1)

if __name__=='__main__': unittest.main()
