import unittest
from unittest.mock import patch, Mock
import main
import qr_sync

QR = 'SGWCMAID' + 'X' * 12 + 'A' * 64

class GroupQRTests(unittest.TestCase):
    def setUp(self):
        groups = patch.object(main, "ALLOWED_GROUP_IDS", frozenset({135990003}))
        groups.start(); self.addCleanup(groups.stop)
        self.event = dict(post_type='message', message_type='group', group_id=135990003,
                          user_id=12345678, self_id=87654321, message_id=10,
                          raw_message='/login ' + QR,
                          message=[{'type':'text','data':{'text':'/login '+QR}}])
        self.calls = []
        self.role = 'admin'
        self.original = dict(self.event)
        self.fail_delete = False
        def api(action, body, **kw):
            self.calls.append(action)
            if action == 'get_group_member_info':
                self.assertEqual(body['user_id'], self.event['self_id'])
                return {'role':self.role}
            if action == 'get_msg': return self.original
            if action == 'delete_msg':
                if self.fail_delete: raise RuntimeError(QR)
                return None
            raise AssertionError(action)
        for p in (patch('socket.socket', side_effect=AssertionError('offline')),
                  patch.object(main,'onebot_api',side_effect=api),
                  patch.object(main,'send_group'),
                  patch.object(qr_sync.DEFAULT_SYNC,'handle')):
            obj = p.start(); self.addCleanup(p.stop)
        self.sender = main.send_group
        self.handler = qr_sync.DEFAULT_SYNC.handle

    def test_admin_recall_before_capture_owner_and_rotation(self):
        def capture(event, send):
            self.assertEqual(self.calls,['get_group_member_info','get_msg','delete_msg'])
            self.assertEqual(event['user_id'],12345678)
            self.assertEqual(event['message_type'],'private')
            send(12345678,'取分完成：1 条成绩；会话已保留，使用 /logout 登出。')
        self.handler.side_effect = capture
        main.handle(self.event)
        self.handler.assert_called_once()
        self.assertIn('轮换二维码',self.sender.call_args.args[1])
        self.assertNotIn(QR,repr(self.sender.call_args_list))

    def test_account_alias_reaches_capture_after_recall(self):
        text = '/绑定账号 ' + QR
        self.event['raw_message'] = text
        self.event['message'] = [{'type': 'text', 'data': {'text': text}}]
        self.original = dict(self.event)
        main.handle(self.event)
        self.assertEqual(self.calls, ['get_group_member_info', 'get_msg', 'delete_msg'])
        self.handler.assert_called_once()
        self.assertEqual(self.handler.call_args.args[0]['raw_message'], text)

    def test_private_account_alias_reaches_capture(self):
        text = '/绑定账号 ' + QR
        event = dict(self.event, message_type='private', raw_message=text,
                     message=[{'type': 'text', 'data': {'text': text}}])
        main.handle(event)
        self.handler.assert_called_once()
        self.assertEqual(self.calls, [])

    def test_no_admin_warns_and_processes_verified_message(self):
        self.role = 'member'
        main.handle(self.event)
        self.assertEqual(self.calls,['get_group_member_info','get_msg'])
        self.handler.assert_called_once()
        self.assertIn('没有群管理员权限',self.sender.call_args.args[1])

    def test_public_help_without_permission_or_credentials(self):
        for text in ('/help', '/帮助', '/login', '/取分', '/token', '/获取token', '/绑定水鱼'):
            self.event['raw_message'] = text
            self.event['message'] = [{'type':'text','data':{'text':text}}]
            main.handle(self.event)
            result = self.sender.call_args.args[1]
            if text in ('/help', '/帮助', '/login', '/取分'):
                for category in ('【登录取分】', '【成绩同步】', '【群聊取分】', '【首次使用】'):
                    self.assertIn(category,result)
                self.assertIn('/token',result)
                self.assertNotIn('https://',result)
            else:
                self.assertIn('https://maimai.lxns.net/user/profile',result)
        self.assertEqual(self.calls,[])
        self.handler.assert_not_called()

    def test_delete_failure_warns_processes_and_redacts(self):
        self.fail_delete = True
        main.handle(self.event)
        self.handler.assert_called_once()
        self.assertIn('撤回失败', self.sender.call_args.args[1])
        self.assertNotIn(QR,repr(self.sender.call_args_list))

    def test_group_controls_without_private_send(self):
        for text in ('/同意HTTP取分', '/撤销HTTP取分', '/logout'):
            self.event['raw_message'] = text
            self.event['message'] = [{'type':'text','data':{'text':text}}]
            with patch.object(main,'send_private',side_effect=AssertionError('no private send')):
                main.handle(self.event)
            forwarded, send = self.handler.call_args.args
            self.assertEqual(forwarded['user_id'],12345678)
            send(12345678,'已保存 HTTP 取分授权。')
        self.assertEqual(self.calls,[])

    def test_group_binding_only_after_recall(self):
        text = '/绑定水鱼 synthetic-secret'
        self.event['raw_message'] = text
        self.event['message'] = [{'type':'text','data':{'text':text}}]
        self.original = dict(self.event)
        def binding(event, send):
            self.assertEqual(self.calls,['get_group_member_info','get_msg','delete_msg'])
            self.assertEqual(event['raw_message'],text)
            self.assertEqual(event['user_id'],12345678)
            send(12345678,'水鱼凭据已绑定。')
            return True
        with patch('site_bindings.handle',side_effect=binding), \
             patch.object(main,'send_private',side_effect=AssertionError('no private send')):
            main.handle(self.event)
        self.handler.assert_not_called()
        self.assertNotIn('synthetic-secret',repr(self.sender.call_args_list))

    def test_failed_recall_warns_but_binds(self):
        text = '/绑定落雪 synthetic-secret'
        self.event['raw_message'] = text
        self.event['message'] = [{'type':'text','data':{'text':text}}]
        self.original = dict(self.event)
        self.fail_delete = True
        with patch('site_bindings.handle') as binding:
            main.handle(self.event)
            binding.assert_called_once()
        self.assertNotIn('synthetic-secret',repr(self.sender.call_args_list))

    def test_mismatched_original_blocks_delete(self):
        self.original['user_id']=22222222
        main.handle(self.event)
        self.assertNotIn('delete_msg',self.calls)
        self.handler.assert_not_called()

    def test_outside_whitelist_no_calls(self):
        self.event['group_id']=1
        main.handle(self.event)
        self.assertEqual(self.calls,[])
        self.sender.assert_not_called()

    def test_mixed_segments_and_token_not_captured(self):
        self.event['message'].append({'type':'at','data':{'qq':'123456'}})
        main.handle(self.event)
        self.handler.assert_not_called()
        self.assertEqual(self.calls,[])
        self.event['message']=[{'type':'text','data':{'text':'/绑定水鱼 secret'}}]
        self.event['raw_message']='/绑定水鱼 secret'
        main.handle(self.event)
        self.handler.assert_not_called()
        self.assertNotIn('secret',repr(self.sender.call_args_list))

if __name__ == '__main__': unittest.main()
