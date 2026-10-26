"""Public examples are templates; never copy private test accounts/VODs here."""
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

    async def test_public_commands_and_format_errors_use_only_template_examples(self):
        allowed_examples = {
            '<VOD_ID>', '<CHANNEL_NAME>', '/vod <VOD_ID>', '/channel <CHANNEL_NAME>',
            'https://www.twitch.tv/videos/<VOD_ID>',
            'https://www.twitch.tv/<CHANNEL_NAME>',
        }
        allowed_urls = {'https://www.twitch.tv/videos/<VOD_ID>',
                        'https://www.twitch.tv/<CHANNEL_NAME>'}
        # Also prevent example logins from slipping into plain text. Keep this
        # allowlist free of private identifiers, including in failure messages.
        allowed_terms = {'Twitch', 'VOD', 'ID', 'URL', 'VOD_ID', 'CHANNEL_NAME',
                         'https', 'www', 'twitch', 'tv', 'videos', 'vod', 'channel', 'help'}
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
                    self.assertTrue(set(parser.examples) <= allowed_examples,
                                    'Public instruction contains a concrete example')
                    urls = {url.rstrip('.,;') for url in re.findall(r'https?://(?:www\.)?twitch\.tv/[^\s]+', plain)}
                    self.assertTrue(urls <= allowed_urls,
                                    'Public instruction contains a concrete Twitch URL')
                    self.assertIsNone(re.search(r'\b[0-9]{5,}\b', plain),
                                      'Public instruction contains a concrete numeric ID')
                    if parser.examples:
                        self.assertTrue(message.reply_text.await_args.kwargs['link_preview_options'].is_disabled)
                    self.assertEqual({}, context.user_data)
            prepare.assert_not_awaited()
            browse.assert_not_awaited()

    async def test_help_renders_literal_placeholders_instead_of_html_tags(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        await commands.help_command(SimpleNamespace(effective_message=message), SimpleNamespace())
        text = message.reply_text.await_args.args[0]
        self.assertIn('&lt;VOD_ID&gt;', text)
        self.assertIn('&lt;CHANNEL_NAME&gt;', text)
        parser = InstructionText()
        parser.feed(text)
        self.assertIn('/vod <VOD_ID>', parser.examples)
        self.assertIn('/channel <CHANNEL_NAME>', parser.examples)


if __name__ == '__main__':
    unittest.main()
