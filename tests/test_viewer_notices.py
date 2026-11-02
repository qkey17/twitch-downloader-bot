import copy
import json
import unittest

from src.html_renderer import render_viewer_html
from src.viewer_data import decode_row
from src.viewer_notices import prepare_notice, recognize_notice
from tests.notice_fixtures import PHRASES, notice_rows


class NoticeTests(unittest.TestCase):
    def test_confirmed_phrases_and_parameters(self):
        expected = ['subscription', 'subscription', 'watch_streak', 'watch_streak',
                    'community_gift', 'gift', 'subscription', 'subscription',
                    'subscription', 'subscription', 'watch_streak', 'watch_streak',
                    'subscription', 'watch_streak', 'watch_streak', 'subscription']
        for text, kind in zip(PHRASES, expected):
            with self.subTest(text=text[:40]):
                n = recognize_notice(text)
                self.assertEqual(n['kind'], kind)
                self.assertEqual(n['system'] + n['comment'], text)
        self.assertEqual(recognize_notice(PHRASES[0])['streak'], 3)
        self.assertEqual(recognize_notice(PHRASES[0])['months'], 17)
        self.assertEqual(recognize_notice(PHRASES[6])['plan'], 'Tier 2')
        self.assertEqual(recognize_notice(PHRASES[7])['plan'], 'Prime')
        self.assertNotIn('months', recognize_notice(PHRASES[8]))
        self.assertEqual(recognize_notice(PHRASES[2])['count'], 240)
        self.assertEqual(recognize_notice(PHRASES[4])['total'], 1)
        self.assertEqual(recognize_notice(PHRASES[5])['recipient'], 'ega1lan_bs')
        self.assertEqual(recognize_notice(PHRASES[11])['actor'], 'ObjectPrototype__proto__')

    def test_short_subscription_and_valid_bounds(self):
        for ending in ['', ' hello. Two sentences!', '\n日本 🎮  ', '\t \n']:
            text = 'a subscribed with Prime.' + ending
            self.assertEqual(recognize_notice(text)['comment'], ending)
        self.assertEqual(recognize_notice("a subscribed at Tier 1. They've subscribed for 1 month!")['months'], 1)
        self.assertEqual(recognize_notice("a watched 100000 consecutive streams and sparked a watch streak!")['count'], 100000)
        self.assertEqual(recognize_notice("a subscribed at Tier 2. They've subscribed for 1200 months!")['months'], 1200)

    def test_ordinary_quotes_partial_unknown_and_invalid_forms_fall_back(self):
        invalid = [None, 0, '', 'I subscribed at Tier 1 yesterday.', 'I watched a stream and got a badge',
                   'I gifted a sub yesterday', 'He said: ' + PHRASES[1], '"' + PHRASES[1] + '"',
                   '>' + PHRASES[1], PHRASES[3][:-1], PHRASES[3].replace('!', '?'),
                   PHRASES[3] + 'no delimiter', 'a subscribed at Tier 3.',
                   'a subscribed at Tier 1', 'a subscribed at Tier 1! comment',
                   "a subscribed at Tier 1. They've subscribed for 2 months",
                   "a subscribed at Tier 1.  They've subscribed for 2 months",
                   "a subscribed at Tier 1. They've subscribed for 0 months!",
                   "a subscribed at Tier 1. They've subscribed for 01 months!",
                   "a subscribed at Tier 1. They've subscribed for 2 month!",
                   "a subscribed at Tier 1. They've subscribed for 1 months!",
                   "a subscribed at Tier 1. They've subscribed for 1201 months!",
                   "a subscribed at Tier 1. They've subscribed for 2 months, currently on a 3 month streak!",
                   'a watched -1 consecutive streams and sparked a watch streak!',
                   'a watched 100001 consecutive streams and sparked a watch streak!',
                   'a watched 1.5 consecutive streams and sparked a watch streak!',
                   PHRASES[4].replace('gifting 1', 'gifting 2'),
                   PHRASES[4].replace('gifting 1', 'gifting 1000001'),
                   PHRASES[5].replace('Tier 1', 'Tier 2'),
                   '<script> subscribed at Tier 1.', 'a raided with 50 viewers!']
        for text in invalid:
            with self.subTest(text=text):
                self.assertIsNone(recognize_notice(text))
                row = dict(text=text, user='author', fragments=[])
                self.assertIs(prepare_notice(row), row)

    def test_exact_template_imitation_is_an_acknowledged_false_positive(self):
        self.assertIsNotNone(recognize_notice('I subscribed at Tier 1.'))

    def test_one_fragment_splits_without_mutating_source_or_author(self):
        for row in notice_rows():
            with self.subTest(text=row['text'][:40]):
                before = copy.deepcopy(row)
                result = prepare_notice(row)
                self.assertEqual(row, before)
                self.assertEqual(result['text'], row['text'])
                self.assertEqual(result['fragments'], row['fragments'])
                self.assertEqual(result['user'], row['user'])
                n = result['notice']
                self.assertEqual(''.join(f['text'] for f in n['comment_fragments']), n['comment'])
        self.assertEqual(prepare_notice(notice_rows()[9])['user'], 'STVOLove')
        self.assertEqual(prepare_notice(notice_rows()[9])['notice']['actor'], 'xSTVOLx')

    def test_emote_split_whitespace_and_unicode_ranges_are_lossless(self):
        prefix = '日本 subscribed at Tier 1.'
        comment = ' 🎮 Kappa\tссылка https://example.invalid/\nЕщё.  '
        row = dict(text=prefix+comment, fragments=[dict(text=prefix+' 🎮 '),
                   dict(text='Kappa', emoteID='25'), dict(text=comment.split('Kappa')[1])])
        n = prepare_notice(row)['notice']
        self.assertEqual(n['comment'], comment)
        self.assertEqual(n['comment_fragments'][1], dict(text='Kappa', emoteID='25'))
        self.assertEqual(''.join(f['text'] for f in n['comment_fragments']), comment)
        # Existing upstream strips joined text, retaining outer whitespace in fragments.
        row = dict(text=(prefix+comment).strip(), fragments=[dict(text='  '+prefix+comment+'\n')])
        n = prepare_notice(row)['notice']
        self.assertEqual(''.join(f['text'] for f in n['comment_fragments']), n['comment'])

    def test_unsafe_fragment_boundaries_fall_back_losslessly(self):
        text = PHRASES[0]
        for fragments in [[dict(text='different')], [dict(text=text, emoteID='25')],
                          [dict(text=text, emote=dict(id='25'))], [dict(text=42)],
                          [None], 'not a list']:
            row = dict(text=text, fragments=fragments)
            before = copy.deepcopy(row)
            self.assertIs(prepare_notice(row), row)
            self.assertEqual(row, before)
        row = dict(text=PHRASES[0], notice={'existing': True})
        self.assertIs(prepare_notice(row), row)

    def test_streaming_compact_round_trip_and_independent_gifts(self):
        rows = notice_rows() + [dict(user='old', text='ordinary', fragments=[])]
        before = copy.deepcopy(rows)
        html = render_viewer_html(iter(rows), '</ScRiPt>', 'channel', 'https://www.twitch.tv/videos/1')
        raw = html.split('<script id="chat-data" type="application/json">')[1].split('</script>')[0]
        self.assertNotIn('<', raw)
        payload = json.loads(raw)
        self.assertEqual(payload['format'], 'tcd-compact-v1')
        decoded = [decode_row(payload, record) for record in payload['rows']]
        self.assertEqual(len(decoded), len(rows))
        for row, result in zip(rows, decoded):
            self.assertEqual({k: v for k, v in result.items() if k != 'notice'}, row)
        self.assertEqual(decoded[4]['t'], decoded[5]['t'])
        self.assertNotEqual(decoded[4]['notice']['kind'], decoded[5]['notice']['kind'])
        self.assertNotIn('notice', decoded[-1])
        self.assertEqual(rows, before)
