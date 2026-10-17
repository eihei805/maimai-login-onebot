import unittest
from upload_commands import parse_command, validate_operation, load_catalog

class ParserTests(unittest.TestCase):
    def setUp(self):
        self.catalog=[dict(id='8',title='True Love Song',type='SD',level=['2','4','7','10'],charts=[{}, {}, {}, {}])]
    def test_title_spaces_and_integer(self):
        result=parse_command('/上传成绩 True Love Song BASIC 1010000',self.catalog)
        self.assertEqual(result,dict(operation='score',music_id=8,level=0,achievement=1010000,title='True Love Song'))
    def test_level_aliases(self):
        for level,index in [('绿',0),('黄',1),('红',2),('紫',3),('MASTER',3),('0',0)]:
            self.assertEqual(parse_command('/上传成绩 8 '+level+' 1000000',self.catalog)['level'],index)
    def test_item_names_and_numbers(self):
        for kind in ('背景板','1'):
            self.assertEqual(parse_command('/上传物品 '+kind+' 201',[]),dict(operation='item',kind=1,item_id=201))
    def test_bad_inputs(self):
        for text in ('/上传物品 999 201','/上传物品 1 -1','/上传物品 1 201 trailing','/上传成绩 8 白 1000000','/上传成绩 8 绿 1010001','/上传成绩 8 绿 101%','/上传成绩'):
            with self.subTest(text=text),self.assertRaises(ValueError):parse_command(text,self.catalog)
    def test_ambiguous_lists_ids(self):
        self.catalog.append(dict(self.catalog[0],id='10008',type='DX'))
        with self.assertRaisesRegex(ValueError,'10008'):
            parse_command('/上传成绩 True Love Song 绿 1010000',self.catalog)
        self.assertEqual(parse_command('/上传成绩 8 绿 1010000',self.catalog)['music_id'],8)
    def test_bool_rejected(self):
        with self.assertRaises(ValueError): validate_operation(dict(operation='item',kind=True,item_id=201))
    def test_actual_catalog(self):
        row=parse_command('/上传成绩 8 绿 1010000',load_catalog())
        self.assertEqual(row['title'],'True Love Song')


class StrictOfflineParserTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [dict(id='17', title='Title With Spaces', type='SD', level=['1'] * 5)]

    def test_every_level_alias(self):
        for index, aliases in enumerate((('basic', 'BASIC', '绿', '基础', '0'),
                                         ('ADVANCED', 'advanced', '黄', '进阶', '1'),
                                         ('EXPERT', 'expert', '红', '专家', '2'),
                                         ('MASTER', 'master', '紫', '大师', '3'),
                                         ('REMASTER', 'Re:MASTER', '白', '4'))):
            for alias in aliases:
                with self.subTest(alias=alias):
                    self.assertEqual(parse_command('/上传成绩 17 ' + alias + ' 1010000', self.catalog)['level'], index)

    def test_casefold_and_zero(self):
        self.assertEqual(parse_command('/上传成绩 title with spaces 绿 0', self.catalog),
                         dict(operation='score', music_id=17, level=0, achievement=0, title='Title With Spaces'))

    def test_all_item_kinds(self):
        from item_upload import KINDS
        for name, kind in KINDS.items():
            for token in (name, str(kind)):
                self.assertEqual(parse_command('/上传物品 ' + token + ' 11', []),
                                 dict(operation='item', kind=kind, item_id=11))

    def test_invalid_numbers_bounds_and_trailing_tokens(self):
        for text in ('/上传成绩 17 绿 -1', '/上传成绩 17 绿 +1',
                     '/上传成绩 17 绿 1.0', '/上传成绩 17 绿 １００',
                     '/上传成绩 17 绿 1 trailing', '/上传成绩 17 绿 1 2',
                     '/上传成绩 17 宴 10', '/上传物品 3 0',
                     '/上传物品 3 2147483648', '/上传物品 3 １',
                     '/上传成绩 ' + 'x' * 512, None, True):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_command(text, self.catalog)

    def test_json_structure_and_no_coercion(self):
        op = parse_command('/上传成绩 17 绿 0', self.catalog)
        for bad in (None, [], {}, dict(op, extra=1), dict(op, music_id='17'),
                    dict(op, level=True), dict(op, achievement=True),
                    dict(op, achievement=1010001), dict(op, title='bad\nname'),
                    dict(op, title=''), dict(op, title=12)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_operation(bad)
        self.assertEqual(validate_operation(op), op)
        self.assertIsNot(validate_operation(op), op)

    def test_safe_error_does_not_echo_reference(self):
        secret = 'private-test-reference'
        with self.assertRaises(ValueError) as error:
            parse_command('/上传成绩 ' + secret + ' 绿 10', self.catalog)
        self.assertNotIn(secret, str(error.exception))

    def test_real_remaster_and_duplicate_titles(self):
        from upload_commands import resolve_music
        catalog = load_catalog()
        self.assertGreater(len(catalog), 1000)
        self.assertEqual(parse_command('/上传成绩 17 白 1010000', catalog)['level'], 4)
        titles = {}
        for row in catalog:
            titles.setdefault(row['title'].casefold(), []).append(row)
        duplicate = next(rows for rows in titles.values() if len(rows) > 1)
        with self.assertRaisesRegex(ValueError, '候选'):
            resolve_music(duplicate[0]['title'], 0, catalog)
        for row in duplicate:
            self.assertEqual(resolve_music(row['id'], 0, catalog)['music_id'], int(row['id']))
