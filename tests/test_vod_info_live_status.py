"""Information cards use only the current available generation's capture fact."""
import json
import time
import unittest
from unittest.mock import AsyncMock, patch
from src.handlers import handlers_vod_info as info
from src.ui import live_status as status
from src.ui.html_file_action import lookup
from src.database import db_results as results
import test_single_message_media as media
import test_result_registry as registry


class InformationCards(unittest.IsolatedAsyncioTestCase):
    for name in ('_setupAsyncioRunner', 'setUp', 'asyncTearDown', 'task', 'api',
                 'message', 'update', 'open', 'select', 'publication', 'worker'):
        locals()[name] = getattr(media.MediaCards, name)

    async def test_current_and_historical_flags_are_independent_in_place(self):
        scenarios = (
            (True, {'captured_during_live': True}, [status.LIVE, status.WARNING]),
            (False, {'captured_during_live': True}, [status.WARNING]),
            (True, None, [status.LIVE]),
            (False, None, []),
            (True, {'captured_during_live': False}, [status.LIVE]),
            (False, {}, []),
            (False, {'captured_during_live': 'true'}, []),
            (False, 'invalid json', []),
        )
        for current, meta, expected in scenarios:
            with self.subTest(current=current, meta=meta):
                message, view = await self.open()
                original_id = message.message_id
                sends = sum(n.startswith('send_') for n, _ in self.calls)
                ready = None if meta is None else dict(id='a'*32, meta_json=json.dumps(meta) if isinstance(meta, dict) else meta)
                with patch.object(info, 'lookup', return_value=ready) as read, \
                     patch.object(info, 'observe', AsyncMock(return_value=dict(value=current, checked_at=time.monotonic()))) as observe:
                    await self.select(message, view)
                read.assert_called_once_with('1')
                observe.assert_awaited_once()
                text = message.caption if message.photo else message.text
                self.assertEqual(expected, text.split(' UTC', 1)[1].strip().splitlines())
                self.assertEqual(sends, sum(n.startswith('send_') for n, _ in self.calls))
                self.assertEqual(original_id, message.message_id)
                self.assertEqual(media.PREVIEW, message.image)
                buttons = [b for row in message.reply_markup.inline_keyboard for b in row]
                self.assertEqual([info.CREATE_CHAT, info.BACK_TO_LIST], [b.text for b in buttons])
                session = media.lists.get_channel_browser(self.app)._sessions[view.session_id]
                self.assertTrue(buttons[0].callback_data.endswith(':process'))
                card = session.info_card
                await info.info_callback(self.update(message, f'ui:vi:{session.token}:{card.nonce}:back'), self.ctx)
                self.assertEqual('channel.png', message.image)
                self.assertEqual(sends, sum(n.startswith('send_') for n, _ in self.calls))


class CurrentGeneration(unittest.TestCase):
    setUp = registry.ResultRegistryTestCase.setUp
    plan = registry.ResultRegistryTestCase.plan
    publish = registry.ResultRegistryTestCase.publish

    def test_only_current_available_generation_without_migration(self):
        old = self.plan(); old.meta['captured_during_live'] = True
        self.publish(old); old.complete(); old.close()
        new = self.plan(); new.meta['captured_during_live'] = False
        self.publish(new); new.complete(); new.close()
        def text():
            ready = lookup(new.vod_id)
            return info.information_text('Channel', 'Title', '1:00', None, ready=ready)
        with patch.object(results, 'get_ready_result', side_effect=AssertionError('migration lookup forbidden')):
            self.assertNotIn(status.WARNING, text())
            # A remains on disk, but an unavailable current B must not select A.
            new.path().unlink()
            self.assertTrue(old.path().is_file())
            self.assertIsNone(lookup(new.vod_id))
            self.assertNotIn(status.WARNING, text())
