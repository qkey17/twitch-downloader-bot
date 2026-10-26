"""Public copy stays neutral, contains no examples, and preserves UI actions."""
from html.parser import HTMLParser
from types import SimpleNamespace
import asyncio
import re
import unittest
from unittest.mock import AsyncMock, patch

from src.handlers import handlers_cmd as commands, handlers_input as inputs


class InstructionText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.examples = []
        self.in_code = False

    def handle_starttag(self, tag, attrs):
        if tag == 'code':
            self.in_code = True
            self.examples.append('')

    def handle_endtag(self, tag):
        if tag == 'code':
            self.in_code = False

    def handle_data(self, data):
        self.text.append(data)
        if self.in_code:
            self.examples[-1] += data


class PublicInstructionPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=asyncio.SelectorEventLoop)

    async def test_public_commands_and_format_errors_contain_no_examples(self):
        # Also prevent example logins from slipping into plain text. Keep this
        # allowlist free of private identifiers, including in failure messages.
        allowed_terms = {'VOD', 'VODs', 'ID', 'vod', 'channel', 'help'}
        cases = (
            (commands.start_command, []), (commands.help_command, []),
            (commands.about_command, []),
            (inputs.vod_command, []), (inputs.channel_command, []),
            (inputs.vod_command, ['fixture_channel']),
            (inputs.channel_command, ['https://www.twitch.tv/videos/123']),
        )
        with patch.object(inputs, 'prepare_vod_choice', AsyncMock()) as prepare, \
             patch.object(inputs, 'show_channel_vods', AsyncMock()) as browse:
            for command, args in cases:
                with self.subTest(command=command.__name__, has_args=bool(args)):
                    message = SimpleNamespace(chat_id=1, reply_text=AsyncMock())
                    update = SimpleNamespace(effective_message=message)
                    context = SimpleNamespace(user_data={}, args=args)
                    await command(update, context)
                    text = message.reply_text.await_args.args[0]
                    parser = InstructionText()
                    parser.feed(text)
                    plain = ''.join(parser.text)
                    self.assertTrue(set(re.findall(r'[A-Za-z_][A-Za-z_0-9]*', plain)) <= allowed_terms,
                                    'Public instruction contains a non-template identifier')
                    self.assertEqual([], parser.examples)
                    urls = {url.rstrip('.,;') for url in re.findall(r'https?://(?:www\.)?twitch\.tv/[^\s]+', plain)}
                    self.assertEqual(set(), urls)
                    self.assertIsNone(re.search(r'(?i)Twitch|архив|запис|видео|пример', plain))
                    self.assertIsNone(re.search(r'\b[0-9]{5,}\b', plain),
                                      'Public instruction contains a concrete numeric ID')
                    if parser.examples:
                        self.assertTrue(message.reply_text.await_args.kwargs['link_preview_options'].is_disabled)
                    self.assertEqual({}, context.user_data)
            prepare.assert_not_awaited()
            browse.assert_not_awaited()

    async def test_commands_match_approved_copy(self):
        cases = (
            (commands.start_command,
             'Вставьте ссылку на VOD или его ID, чтобы получить чат.\n\n'
             'Вставьте имя канала или ссылку на него, чтобы посмотреть список VODs.\n\nПодробнее — /help.'),
            (commands.help_command,
             'Бот автоматически определяет тип запроса.\n\n'
             '/vod — получить чат VOD.\n/channel — посмотреть VODs канала.\n\n'
             'Команды позволяют указать тип запроса вручную.'),
            (inputs.vod_command, 'Вставьте ссылку на VOD или его ID.'),
            (inputs.channel_command, 'Вставьте имя канала или ссылку на него.'),
        )
        for command, expected in cases:
            with self.subTest(command=command.__name__):
                message = SimpleNamespace(reply_text=AsyncMock())
                await command(SimpleNamespace(effective_message=message), SimpleNamespace(args=[]))
                self.assertEqual(expected, message.reply_text.await_args.args[0])
                self.assertNotIn('reply_markup', message.reply_text.await_args.kwargs)
                self.assertTrue(message.reply_text.await_args.kwargs['link_preview_options'].is_disabled)

    def test_channel_copy_preserves_user_data_counters_and_button_actions(self):
        from src.channel_browser import BrowseView
        from src.twitch_api.videos import ArchiveVod, TwitchUser
        from src.ui.ui_channel_vods import build_channel_vod_card, _MESSAGES
        expected_messages = {
            'loading': 'Загрузка VODs канала…',
            'empty': 'У канала нет опубликованных VODs.',
            'empty_page': 'На этой странице нет VODs. Перейдите дальше.',
            'not_found': 'Канал не найден. Проверьте имя или ссылку.',
            'api_error': 'Не удалось открыть список VODs.',
            'rate_limited': 'Слишком много запросов. Повторите попытку позже.',
            'limit': 'Достигнут лимит списка. Показаны загруженные VODs.',
            'capacity': 'Слишком много запросов. Повторите попытку позже.',
            'busy': 'Дождитесь завершения предыдущего запроса.',
            'expired': 'Список устарел. Откройте его заново.',
            'stale': 'Кнопка устарела. Используйте текущую карточку VOD.',
            'forbidden': 'Эта кнопка недоступна.',
            'invalid': 'Просмотр недоступен. Откройте список VODs заново.',
            'selected': 'VOD выбран.',
        }
        self.assertEqual(expected_messages, _MESSAGES)
        # Channel/VOD names are external data: do not rewrite their wording.
        channel = TwitchUser('1', 'fixture_channel', 'Twitch-канал <&>')
        vod = ArchiveVod('2', '1', channel.login, channel.display_name, 'Архив: запись и видео',
                         '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '1m', '', '', 'public')
        for status in _MESSAGES:
            with self.subTest(status=status):
                view = BrowseView(status, session_id='a'*16, revision=4, channel=channel, vod=vod,
                                  position=1, loaded=2, has_previous=True, has_next=True)
                text, keyboard = build_channel_vod_card(view)
                self.assertIn('🟣 Twitch-канал &lt;&amp;&gt;', text)
                self.assertIn('🎬 Архив: запись и видео', text)
                self.assertIn('📚 VODs канала: 2+', text)
                self.assertEqual('Выбрать VOD', keyboard.inline_keyboard[0][0].text)
                self.assertEqual('ui:cv:'+'a'*16+':4:select:2', keyboard.inline_keyboard[0][0].callback_data)
                self.assertEqual('🔗 Открыть VOD', keyboard.inline_keyboard[0][1].text)
                self.assertEqual('https://www.twitch.tv/videos/2', keyboard.inline_keyboard[0][1].url)
                self.assertEqual(['⬅️', '2/2+', '➡️'], [b.text for b in keyboard.inline_keyboard[1]])

    def test_progress_does_not_claim_html_is_ready_after_chat_download(self):
        from src.ui.ui_texts import build_progress_text
        from src.workers.worker_progress import _build_progress_card
        meta = {'channel': 'fixture_channel', 'title': 'Title', 'length_seconds': 60, 'vod_len': '00:01:00'}
        for done in (False, True):
            expected = 'Чат загружен. Подготовка HTML…' if done else 'Загрузка чата…'
            for text in (_build_progress_card(meta, 'fixture-url', 17, 4, done),
                         build_progress_text(meta, 'fixture-url', 'html_online', 17, 4, 0, 2, done)):
                self.assertIn(expected, text)
                self.assertNotIn('Готово', text)
                self.assertIn('Title', text)
                self.assertIn('17', text)
                self.assertIn('4', text)

    def test_button_labels_joke_and_internal_cancel_marker_are_preserved(self):
        from src.handlers.handlers_vod import _FORMAT_PROMPT_TEXT
        from src.handlers.handlers_state import CANCELLED_MESSAGE
        from src.ui.ui_keyboards import build_format_keyboard
        from src.ui.ui_labels import CHAT_GENERIC, FILES, VOD_LINK
        self.assertEqual(
            'В современной культуре термин «казуал» иногда используется в пренебрежительном контексте, '
            'однако чаще всего это просто описание стиля игры — легкого и непринужденного, без фанатизма.',
            _FORMAT_PROMPT_TEXT)
        self.assertEqual('Загрузка была отменена.', CANCELLED_MESSAGE)
        keyboard = build_format_keyboard().inline_keyboard
        self.assertEqual([['🌐 HTML'], ['❌ Отмена']], [[b.text for b in row] for row in keyboard])
        self.assertEqual([['vodfmt:html_online'], ['ui:format_cancel']],
                         [[b.callback_data for b in row] for row in keyboard])
        self.assertEqual(('📖 Открыть чат', '📁 Получить файлы', '🔗 Открыть VOD'),
                         (CHAT_GENERIC, FILES, VOD_LINK))


if __name__ == '__main__':
    unittest.main()
