"""Local input cases; no bot, credentials, network, or user database."""
import unittest
from unittest.mock import patch

from src.input_classifier import ClassifiedInput, InputClassificationError, classify_input


class InputClassifierTests(unittest.TestCase):
    def test_existing_vod_url_forms_keep_their_supported_suffixes(self):
        for scheme in ('https://', 'http://', ''):
            for host in ('twitch.tv', 'www.twitch.tv'):
                for path in ('videos/123456789', 'channel/v/123456789'):
                    for suffix in ('', '/', '?t=1h#chat', '/?t=1h'):
                        text = scheme + host + '/' + path + suffix
                        with self.subTest(text=text):
                            self.assertEqual(ClassifiedInput('vod', '123456789'), classify_input(text))

    def test_vod_ids_are_normalized_and_take_precedence_over_logins(self):
        for text, value in (
            ('123456789', '123456789'), ('  000123  ', '123'), ('1', '1'),
            ('9'*20, '9'*20), ('\nhttps://WWW.TWITCH.TV/videos/000123/\t', '123'),
            ('HTTP://TWITCH.TV/channel/v/123', '123'),
        ):
            with self.subTest(text=text):
                self.assertEqual(ClassifiedInput('vod', value), classify_input(text))
        for text in ('0', '000', '9'*21, '9'*25, 'https://twitch.tv/videos/0',
                     'https://twitch.tv/videos/'+'9'*21):
            with self.subTest(text=text), self.assertRaises(InputClassificationError):
                classify_input(text)

    def test_channel_login_syntax_case_and_outer_whitespace(self):
        for text, login in (
            ('channel_name', 'channel_name'), ('  ChAnNeL_123  ', 'channel_123'),
            ('a', 'a'), ('ab', 'ab'), ('A'*25, 'a'*25), ('123abc', '123abc'),
            ('\tchannel\n', 'channel'),
        ):
            with self.subTest(text=text):
                self.assertEqual(ClassifiedInput('channel', login), classify_input(text))

    def test_channel_urls_are_exact_twitch_hosts_and_single_login_paths(self):
        for scheme in ('https://', 'http://', ''):
            for host in ('twitch.tv', 'www.twitch.tv', 'WWW.TWITCH.TV'):
                for suffix in ('', '/'):
                    text = scheme + host + '/ChAnNeL_123' + suffix
                    with self.subTest(text=text):
                        self.assertEqual(ClassifiedInput('channel', 'channel_123'), classify_input(text))
        self.assertEqual(ClassifiedInput('channel', '123'), classify_input('https://twitch.tv/123'))

    def test_command_prefix_is_not_confused_with_a_channel_login(self):
        for command in ('start', 'about', 'history', 'cancel'):
            with self.subTest(command=command):
                self.assertEqual(ClassifiedInput('channel', command), classify_input(command))
                self.assertEqual(ClassifiedInput('channel', command), classify_input('https://twitch.tv/'+command))
                for text in ('/'+command, '/'+command+'@some_bot', '/'+command+' channel'):
                    with self.subTest(text=text), self.assertRaises(InputClassificationError):
                        classify_input(text)

    def test_non_channel_twitch_routes_and_clips_do_not_become_logins(self):
        for text in (
            'https://clips.twitch.tv/ClipSlug', 'https://www.twitch.tv/channel/clip/ClipSlug',
            'https://twitch.tv/clip/ClipSlug', 'https://twitch.tv/videos',
            'https://twitch.tv/clips', 'https://twitch.tv/directory/',
            'https://twitch.tv/search', 'https://twitch.tv/settings',
        ):
            with self.subTest(text=text), self.assertRaises(InputClassificationError):
                classify_input(text)

    def test_foreign_and_malformed_urls_are_rejected_without_login_fallback(self):
        for text in (
            'https://example.com/channel', 'https://example.com/videos/123', 'example.com',
            'https://twitch.tv.evil/channel', 'https://twitch.tv@evil/channel',
            'https://evil@twitch.tv/channel', 'https://www.twitch.tv:443/channel',
            'https://twitch.tv:bad/channel', 'https://twitch.tv./channel',
            'ftp://twitch.tv/channel', '//twitch.tv/channel', '/channel',
            'https://[invalid/channel', 'https://twitch.tv', 'twitch.tv/',
            'https://twitch.tv/channel/extra', 'https://twitch.tv/channel//',
            'https://twitch.tv//channel', 'https://twitch.tv/%63hannel',
            'https://twitch.tv/channel?sr=a', 'https://twitch.tv/channel#about',
            'https://twitch.tv/channel?', 'https://twitch.tv/channel#',
            'https://twitch.tv/videos/123abc', 'https://twitch.tv/videos/123/extra',
            'https://twitch.tv/videos/123//', 'https://twitch.tv/channel/v/123/v/123',
            'https://twitch.tv/?videos=123', 'https://twitch.tv/Videos/123',
            'https://twitch.tv/videos/１２３',
        ):
            with self.subTest(text=text), self.assertRaises(InputClassificationError):
                classify_input(text)

    def test_unsupported_text_unicode_and_internal_whitespace_are_rejected(self):
        for text in (
            '', ' ', 'two words', 'abc 123456789', '@channel', 'channel-name',
            'channel.name', 'x'*26, 'канал', '１２３', 'Kelvin', 'İname',
            'chan\nnel', 'https://twit\tch.tv/channel', 'https://twitch.tv/chan\nnel',
            '\x00channel', 'channel\x7f', None, 123, False, [],
        ):
            with self.subTest(text=text), self.assertRaises(InputClassificationError):
                classify_input(text)

    def test_classification_uses_no_http_sqlite_or_credentials(self):
        with patch('sqlite3.connect', side_effect=AssertionError('SQLite used')) as sql, \
             patch('aiohttp.ClientSession', side_effect=AssertionError('HTTP used')) as http, \
             patch('src.config.get_twitch_credentials', side_effect=AssertionError('credentials used')) as credentials:
            for text in ('channel', '123', 'https://twitch.tv/channel', 'https://twitch.tv/videos/123'):
                with self.subTest(text=text):
                    classify_input(text)
            sql.assert_not_called()
            http.assert_not_called()
            credentials.assert_not_called()


if __name__ == '__main__':
    unittest.main()
