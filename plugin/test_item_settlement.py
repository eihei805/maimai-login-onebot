"""Offline only: synthetic sessions, inventory pages and settlement fragments."""
import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from item_upload import attach_to_settlement

spec = importlib.util.spec_from_file_location('item_protocol_test', Path(__file__).resolve().parent.parent / 'maimai-minimal-client.py')
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def row(i, stock=1):
    return dict(itemKind=3, itemId=i, stock=stock, isValid=True)


class SettlementTests(unittest.TestCase):
    def request(self):
        return dict(userId=1, playlogId=2, loginDateTime=3, isEventMode=False,
                    isFreePlay=False, userPlaylogList=[{'synthetic': True}],
                    upsertUserAll=dict(userData=[{'preserved': 1}], userExtend=[{'preserved': 2}],
                        userOption=[{'preserved': 3}], userRatingList=[{'preserved': 4}],
                        userGamePlaylogList=[{'playlogId': 2}], userMusicDetailList=[{'synthetic': True}],
                        userItemList=[row(1, 4), row(2)], isNewItemList='01'))

    def attach(self, req, **kw):
        args = dict(user_id=1, login_id=2, login_date_time=3)
        args.update(kw)
        return attach_to_settlement(req, [row(1)], 3, [3], **args)

    def test_preserve_existing_settlement_and_pending_items(self):
        req = self.request()
        original = copy.deepcopy(req)
        result = self.attach(req)
        self.assertEqual(req, original)
        self.assertEqual(result['upsertUserAll']['userItemList'], [row(1, 4), row(2), row(3)])
        self.assertEqual(result['upsertUserAll']['isNewItemList'], '011')
        result['upsertUserAll']['userItemList'] = original['upsertUserAll']['userItemList']
        result['upsertUserAll']['isNewItemList'] = '01'
        self.assertEqual(result, original)

    def test_wrong_session_and_missing_gameplay(self):
        for kw in ({'login_id': 9}, {'user_id': True}, {'login_date_time': 4}):
            with self.assertRaises(ValueError): self.attach(self.request(), **kw)
        for key in ('userPlaylogList',):
            req = self.request(); req[key] = []
            with self.assertRaises(ValueError): self.attach(req)
        for key in ('userExtend', 'userGamePlaylogList', 'userMusicDetailList'):
            req = self.request(); req['upsertUserAll'].pop(key)
            with self.assertRaises(ValueError): self.attach(req)
        req = self.request(); req['upsertUserAll']['isNewItemList'] = '11'
        with self.assertRaises(ValueError): self.attach(req)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(m.Client)
        self.session = m.Session(1, 2, 3, 'synthetic', '1')
        self.client.session = self.session
        self.calls = []

    def fetch(self, replies, budget=128):
        replies = iter(replies)
        def game(api, payload, identity):
            self.calls.append((api, payload, identity))
            return next(replies)
        self.client._game = game
        return self.client.item_inventory(self.session, 3, budget)

    def page(self, rows, cursor=0):
        return dict(userId=1, itemKind=3, userItemList=rows, nextIndex=cursor)

    def test_returned_cursor_and_complete_rows(self):
        self.assertEqual(self.fetch([self.page([row(1)], 30000000017), self.page([row(2)])]), [row(1), row(2)])
        self.assertEqual(self.calls[0][1]['nextIndex'], 30000000000)
        self.assertEqual(self.calls[1][1]['nextIndex'], 30000000017)
        self.assertTrue(all(x[0] == 'GetUserItemApi' for x in self.calls))

    def test_reject_partial_cycle_duplicate_cross_kind(self):
        cases = [[self.page([row(1)], 30000000000)],
                 [self.page([row(1)], 30000000001), self.page([row(1)])],
                 [self.page([], 30000000001)], [self.page([row(1)], 20000000001)],
                 [dict(self.page([]), userId=True)], [self.page([row(1, True)])]]
        for replies in cases:
            with self.subTest(replies=replies), self.assertRaises(m.ProtocolError): self.fetch(replies)
        with self.assertRaises(m.ProtocolError): self.fetch([self.page([row(1)], 30000000001)], 1)
        with self.assertRaises(m.ProtocolError): self.client.item_inventory(None, 3)


if __name__ == '__main__':
    unittest.main()
