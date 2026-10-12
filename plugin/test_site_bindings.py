import json
import os
from pathlib import Path
from types import SimpleNamespace, ModuleType
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from site_bindings import BindingError, BindingStore, SiteBindings


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'private'
        self.bindings = SiteBindings(self.root)
        self.sent = []
        self.send = lambda qq, text: self.sent.append((qq, text))

    def tearDown(self):
        self.temp.cleanup()

    def event(self, text, qq=123456, **extra):
        return dict(post_type='message', message_type='private', user_id=qq,
                    raw_message=text, **extra)

    def test_bind_status_unbind_isolation_modes(self):
        for text in ('/绑定水鱼 synthetic-fish', '/绑定落雪 synthetic-lxns', '/上传状态'):
            self.assertTrue(self.bindings.handle(self.event(text), self.send))
        self.assertEqual(self.bindings.credentials('123456'),
                         {'fish_import_token': 'synthetic-fish', 'lxns_personal_token': 'synthetic-lxns'})
        self.assertEqual(self.bindings.credentials('987654'), {})
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'qq-123456.json').stat().st_mode & 0o777, 0o600)
        self.bindings.handle(self.event('/解绑水鱼'), self.send)
        self.assertEqual(self.bindings.credentials('123456'), {'lxns_personal_token': 'synthetic-lxns'})
        self.bindings.handle(self.event('/解绑落雪'), self.send)
        self.assertFalse((self.root / 'qq-123456.json').exists())
        self.assertNotIn('synthetic-', repr(self.sent))

    def test_groups_and_bad_qq_do_not_touch_storage(self):
        event = self.event('/绑定水鱼 secret')
        event['message_type'] = 'group'
        self.assertFalse(self.bindings.handle(event, self.send))
        for qq in ('../123456', '012345', '1234', '1234567890123', True, '１２３４５', '12345\n'):
            self.assertTrue(self.bindings.handle(self.event('/绑定水鱼 secret', qq), self.send))
        self.assertFalse(self.root.exists())
        self.assertEqual(self.sent, [])

    def test_token_help_without_credential_or_storage(self):
        for command in ('/绑定', '/绑定水鱼', '/绑定落雪', '/token', '/获取token'):
            self.assertTrue(self.bindings.handle(self.event(command), self.send))
            self.assertIn('https://maimai.diving-fish.com/', self.sent[-1][1])
            self.assertIn('https://maimai.lxns.net/user/profile', self.sent[-1][1])
        self.assertFalse(self.root.exists())
        event = self.event('/token')
        event['message_type'] = 'group'
        self.assertFalse(self.bindings.handle(event, self.send))

    def test_invalid_tokens_fixed_response(self):
        for token in ('', 'a b', 'a\r', 'a\n', 'a\x00', 'a\t', 'é', 'x' * 2049):
            self.bindings.handle(self.event('/绑定水鱼 ' + token), self.send)
            self.assertFalse(self.root.exists())
        self.bindings.handle(self.event('/绑定水鱼 ' + 'x' * 2048), self.send)
        self.assertEqual(len(self.bindings.credentials('123456')['fish_import_token']), 2048)
        self.assertNotIn('x' * 100, repr(self.sent))

    def test_text_segments(self):
        event = self.event('irrelevant', message=[{'type': 'text', 'data': {'text': '/绑定水鱼 test-only'}}])
        self.assertTrue(self.bindings.handle(event, self.send))
        self.assertEqual(self.bindings.credentials('123456')['fish_import_token'], 'test-only')
        event['message'].append({'type': 'image', 'data': {}})
        self.assertFalse(self.bindings.handle(event, self.send))

    def test_symlink_hardlink_permissions_caps(self):
        self.bindings.store.set('123456', 'fish_import_token', 'test-only')
        target = self.root / 'qq-123456.json'
        outside = Path(self.temp.name) / 'outside'
        outside.write_text('outside-safe')
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaises(OSError):
            self.bindings.store.get('123456')
        with self.assertRaises(OSError):
            self.bindings.store.set('123456', 'fish_import_token', 'new')
        self.assertEqual(outside.read_text(), 'outside-safe')
        target.unlink()
        os.link(outside, target)
        with self.assertRaises(BindingError):
            self.bindings.store.get('123456')
        target.unlink()
        target.write_bytes(b' ' * 17000)
        target.chmod(0o600)
        with self.assertRaises(BindingError):
            self.bindings.store.get('123456')
        target.write_text('{}')
        target.chmod(0o644)
        with self.assertRaises(BindingError):
            self.bindings.store.get('123456')
        self.root.chmod(0o755)
        with self.assertRaises(BindingError):
            self.bindings.store.get('123456')

    def test_symlink_directory_and_lock(self):
        actual = Path(self.temp.name) / 'actual'
        actual.mkdir(mode=0o700)
        self.root.symlink_to(actual)
        with self.assertRaises(BindingError):
            self.bindings.store.get('123456')
        self.root.unlink()
        self.root.mkdir(mode=0o700)
        (self.root / '.bindings.lock').symlink_to(actual / 'lock')
        with self.assertRaises(OSError):
            self.bindings.store.get('123456')

    def test_concurrent_store_updates(self):
        errors = []
        def write(site):
            try:
                BindingStore(self.root).set('123456', site, 'synthetic')
            except Exception as exc:
                errors.append(type(exc).__name__)
        jobs = [threading.Thread(target=write, args=(site,))
                for site in ('fish_import_token', 'lxns_personal_token')]
        for job in jobs: job.start()
        for job in jobs: job.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.bindings.credentials('123456')), 2)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_atomic_failure_keeps_previous(self):
        self.bindings.store.set('123456', 'fish_import_token', 'old-synthetic')
        with patch('site_bindings.os.replace', side_effect=OSError('sensitive exception')):
            self.bindings.handle(self.event('/绑定水鱼 new-synthetic'), self.send)
        self.assertEqual(self.bindings.credentials('123456')['fish_import_token'], 'old-synthetic')
        self.assertEqual(list(self.root.glob('*.tmp')), [])
        self.assertNotIn('sensitive exception', repr(self.sent))

    def test_callback_complete_only_explicit_adapter_safe_output(self):
        self.bindings.store.set('123456', 'fish_import_token', 'synthetic-fish')
        result = SimpleNamespace(requester='123456', scores=[{}], summary={'status': 'partial'})
        result.upload_context = self.bindings.snapshot('123456')
        calls = []
        def adapter(module, capture, bindings, catalogs):
            calls.append((capture, bindings, catalogs))
            return {'fish': {'state': 'success'}, 'lxns': {'state': 'synthetic-fish'}}
        self.bindings.configure_upload(uploader=adapter, catalog_provider=lambda result: ('fish', 'lxns'))
        with patch.dict(sys.modules, {'score_upload': ModuleType('score_upload')}):
            self.bindings.upload_callback(result, self.send)
            self.assertEqual(calls, [])
            result.summary['status'] = 'complete'
            self.bindings.upload_callback(result, self.send)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], {'fish_import_token': 'synthetic-fish'})
        self.assertNotIn('synthetic-fish', repr(self.sent))

    def test_callback_missing_configuration(self):
        result = SimpleNamespace(requester='123456', scores=[], summary={'status': 'complete'})
        result.upload_context = self.bindings.snapshot('123456')
        self.bindings.upload_callback(result, self.send)
        self.assertIn('未配置上传凭据', self.sent[-1][1])
        self.bindings.store.set('123456', 'lxns_personal_token', 'synthetic')
        result.upload_context = self.bindings.snapshot('123456')
        self.bindings.upload_callback(result, self.send)
        self.assertIn('上传适配器尚未配置', self.sent[-1][1])


if __name__ == '__main__':
    unittest.main()
