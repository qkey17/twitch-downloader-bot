import ast
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from src.handlers.handlers_vod_info import preview_url
import test_vod_list as lists


class SmallFixTests(unittest.TestCase):
    def test_preview_templates_and_saved_concrete_url(self):
        for pattern in ['%{width}x%{height}','{width}x{height}']:
            self.assertEqual('https://static-cdn.jtvnw.net/cf_vods/x/thumb/custom-720x405.jpg',
                preview_url('https://static-cdn.jtvnw.net/cf_vods/x/thumb/custom-'+pattern+'.jpg'))
        saved='https://static-cdn.jtvnw.net/cf_vods/x/thumb/custom-480x270.jpg'
        self.assertEqual(saved,preview_url(saved))
        for value in [None,'','javascript:alert(1)','https://evil.invalid/x.jpg',
                      'https://vod-secure.twitch.tv/_404/404_processing_%{width}x%{height}.png']:
            self.assertIsNone(preview_url(value))

    def test_command_registration_and_menu(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'src/main.py').read_text(encoding='utf-8'))
        commands=[n.args[0].value for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='CommandHandler']
        self.assertEqual(['start','help','about','cancel','vod','channel'],commands)
        menu=[n.args[0].value for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='BotCommand']
        self.assertEqual(['help'],menu)


class LiveDateTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner=lists.ListTests._setupAsyncioRunner
    setUp=lists.ListTests.setUp
    asyncTearDown=lists.ListTests.asyncTearDown
    open=lists.ListTests.open
    task=lists.ListTests.task

    async def test_live_button_keeps_date_order_marker_and_callback(self):
        view=await self.open(2)
        text,normal=lists.build_list(view)
        _,live=lists.build_list(replace(view,live_vod_ids=frozenset([view.items[0].id])))
        self.assertEqual('2026-01-02 03:04 🔴 (LIVE)',live.inline_keyboard[0][0].text)
        self.assertEqual(normal.inline_keyboard[0][0].callback_data,live.inline_keyboard[0][0].callback_data)
        self.assertEqual(normal.inline_keyboard[1][0],live.inline_keyboard[1][0])
        self.assertIn('Время VODs указано в UTC.',text)
