import copy
import unittest

from feishu_auth_kit.weixin_ilink import build_text_reply, parse_text_turn


class ILinkContractTest(unittest.TestCase):
    def setUp(self):
        self.message = {
            'message_type': 1, 'message_state': 2, 'message_id': '18446744073709551615',
            'from_user_id': 'owner', 'to_user_id': 'bot', 'context_token': 'test-secret',
            'item_list': [{'type': 1, 'text_item': {'text': 'hello\n  code'}}],
        }

    def parse(self, message=None, allowed=None):
        return parse_text_turn(
            self.message if message is None else message,
            account_id='bot', allowed_senders={'owner'} if allowed is None else allowed,
        )

    def test_preserves_id_and_text(self):
        turn = self.parse()
        self.assertEqual(turn.message_id, '18446744073709551615')
        self.assertEqual(turn.text, 'hello\n  code')
        self.assertEqual(turn.to_dict()['source'], 'weixin_ilink')

    def test_secret_excluded(self):
        turn = self.parse()
        self.assertNotIn('test-secret', repr(turn))
        self.assertNotIn('test-secret', str(turn.to_dict()))

    def test_reply_is_bound(self):
        reply = build_text_reply(self.parse(), 'answer', client_id='host-operation-1')['msg']
        self.assertEqual(reply['to_user_id'], 'owner')
        self.assertEqual(reply['context_token'], 'test-secret')
        self.assertEqual(reply['message_type'], 2)

    def test_denies_missing_allowlist(self):
        with self.assertRaises(ValueError):
            self.parse(allowed=set())

    def test_wrong_fields_fail_closed(self):
        for field, value in [('message_type', 2), ('message_type', True),
                             ('message_state', 1), ('to_user_id', 'other-bot'),
                             ('from_user_id', 'stranger'), ('message_id', 123),
                             ('context_token', ''), ('group_id', 'group')]:
            with self.subTest(field=field, value=value):
                bad = dict(self.message, **{field: value})
                with self.assertRaises(ValueError):
                    self.parse(bad)

    def test_media_and_quotes_rejected(self):
        for item in [{'type': 2}, {'type': True, 'text_item': {'text': 'x'}},
                     {'type': 1, 'text_item': {'text': 'x'}, 'ref_msg': {'svr_id': 'old'}},
                     {'type': 1, 'text_item': {'text': ''}}]:
            bad = copy.deepcopy(self.message)
            bad['item_list'].append(item)
            with self.assertRaises(ValueError):
                self.parse(bad)

    def test_empty_reply_rejected(self):
        with self.assertRaises(ValueError):
            build_text_reply(self.parse(), '', client_id='id')
        with self.assertRaises(ValueError):
            build_text_reply(self.parse(), 'ok', client_id='')

    def test_account_scope(self):
        with self.assertRaises(ValueError):
            parse_text_turn(self.message, account_id='other', allowed_senders={'owner'})


if __name__ == '__main__':
    unittest.main()
