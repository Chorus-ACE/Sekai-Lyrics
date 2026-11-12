import copy
import json
import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import update_english as english
from test_update_lyrics import document


def table(pairs, credit='English translation by Example Translator.'):
    return '<table id="lyrics-1"><tr><th class="lyrics-japanese">Japanese lyrics</th><th class="lyrics-english">English translation</th></tr>' + ''.join(
        f'<tr><td class="lyrics-japanese">{ja}</td><td class="lyrics-english">{en}</td></tr>' for ja,en in pairs
    ) + f'<tr><th colspan="2">{credit}</th></tr></table>'


class EnglishTests(unittest.TestCase):
    def test_ruby_readings_footnotes_and_singer_names_do_not_contaminate_text(self):
        html = table([('<span title="Singer"><ruby>未来<rt>みらい</rt></ruby></span><sup>[1]</sup>', 'Future')])
        self.assertEqual(english.translation_tables(html)[0][0], [('未来','Future')])
        self.assertEqual(english.translation_tables('<table><tr><th>English</th><td>Song title</td></tr></table>'), [])

    def test_multiline_cells_keep_line_boundaries_and_missing_lines_do_not_shift(self):
        parsed = english.translation_tables(table([('未来<br>世界', 'Future<br>World'), ('昨日<br>明日', 'Yesterday')]))
        self.assertEqual(parsed[0][0], [('未来','Future'),('世界','World')])
        self.assertIsNone(english.aligned_translation([{'japanese':'昨日'}], parsed))

    def test_exact_rendition_precedes_projection_and_conflicts_are_not_guessed(self):
        lines = [{'japanese':'未来'}]
        full = ([('昨日','Yesterday'),('未来','A future')], [])
        game = ([('未来','Future')], [])
        self.assertEqual(english.aligned_translation(lines, [full,game])[0], ['Future'])
        self.assertIsNone(english.aligned_translation(lines, [game,([('未来','Different')],[])]))
        repeated = ([('未来','Future'),('未来','Different')],[])
        self.assertIsNone(english.aligned_translation(lines, [repeated]))

    def test_enrichment_preserves_upstream_content_and_adds_component_attribution_and_credits(self):
        doc = document()
        doc['renditions'][0]['game'] = copy.deepcopy(doc['renditions'][0]['full'])
        doc['renditions'][0]['availableVersions'].append('game')
        original = copy.deepcopy(doc)
        tables = english.translation_tables(table([('テスト','Test')]))
        result,count = english.enrich(doc,lambda _: tables)
        self.assertEqual(doc, original)
        self.assertEqual(count,2)
        self.assertEqual(result['renditions'][0]['full']['lines'][0]['zh-CN'],'测试')
        self.assertEqual(result['renditions'][0]['full']['lines'][0]['en-US'],'Test')
        self.assertIn('Example Translator',result['renditions'][0]['englishTranslationCredits']['translation'])
        self.assertEqual(result['renditions'][0]['provenance'][-1]['component'],'renditions/sekai/game_english_translation')
        self.assertEqual(english.enrich(result,lambda _: self.fail('Already translated lines must not refetch')),(result,0))

    def test_nonmatching_sources_existing_english_and_failed_downloads_are_preserved(self):
        doc=document()
        result,count=english.enrich(doc,lambda _:english.translation_tables(table([('別の歌','Other song')])))
        self.assertEqual((result,count),(doc,0))
        doc['renditions'][0]['full']['lines'][0]['en-US']='Existing'
        self.assertEqual(english.enrich(doc,lambda _: self.fail('Do not overwrite upstream English')),(doc,0))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            path=root/'lyrics/music_1.json'
            english.write_json(path,document())
            before=path.read_bytes()
            def broken(_):raise OSError('Unavailable')
            self.assertTrue(english.update(root,fetch=broken)['failures'])
            self.assertEqual(path.read_bytes(),before)

    def test_source_license_is_validated_before_import(self):
        doc=document();doc['renditions'][0]['provenance'][0]['licenseName']='Unsupported'
        with self.assertRaises(ValueError):english.enrich(doc,lambda _: [])

    def test_wrong_song_provenance_is_reported_without_import_or_blocking_other_songs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            path=root/'lyrics/music_1.json'
            english.write_json(path,document())
            before=path.read_bytes()
            def wrong(_):raise english.SourceIdentityMismatch('Source revision belongs to another song')
            report=english.update(root,fetch=wrong)
            self.assertFalse(report['failures'])
            self.assertEqual(report['skipped'][0]['id'],1)
            self.assertEqual(path.read_bytes(),before)

    def test_cached_fixed_revision_identity_and_restriction_markers(self):
        source = document()['renditions'][0]['provenance'][0]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            path = cache / 'sekaipedia-1.json'
            page = {'parse': {'revid': 1, 'title': 'Example', 'wikitext': '{{NoReprint}}',
                              'text': table([('テスト','Test')])}}
            path.write_text(json.dumps(page))
            self.assertEqual(english.fetch_page(source, cache), [])
            page['parse']['wikitext'] = 'Public lyric source'
            path.write_text(json.dumps(page))
            self.assertTrue(english.fetch_page(source, cache))
            page['parse']['revid'] = 2
            path.write_text(json.dumps(page))
            with self.assertRaises(ValueError):english.fetch_page(source, cache)
            page['parse']['revid'] = 1
            page['parse']['wikitext'] = ''
            path.write_text(json.dumps(page))
            with self.assertRaises(ValueError):english.fetch_page(source, cache)
