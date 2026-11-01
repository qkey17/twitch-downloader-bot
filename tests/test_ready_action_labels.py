import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from src.ui.ui_labels import ready_chat_label
from src.handlers.handlers_vod import _offer_ready
import test_result_registry as registry

NORMAL='Получить готовый чат'
PARTIAL='Получить готовый неполный чат'


class SavedLabels(unittest.TestCase):
    def test_strict_saved_fact_and_legacy_malformed_fallback(self):
        for value in [None,{}, {'meta':{}},{'meta':{'is_live':True}},
                      {'meta_json':'bad'},{'meta_json':'[]'},{'meta_json':'null'},
                      {'meta':{'captured_during_live':'true'}},
                      {'meta':{'captured_during_live':1}},
                      {'meta':{'captured_during_live':False,'is_live':True}}]:
            with self.subTest(value=value):self.assertEqual(NORMAL,ready_chat_label(value))
        for value in [{'meta':{'captured_during_live':True,'is_live':False}},
                      {'meta_json':'{"captured_during_live":true}'}]:
            self.assertEqual(PARTIAL,ready_chat_label(value))


class DirectLabels(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        self._asyncioRunner=asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)

    async def test_direct_and_edit_offer_keep_payloads_and_saved_generation(self):
        for flag,expected in [(True,PARTIAL),(False,NORMAL),(None,NORMAL)]:
            for edit in [False,True]:
                ready={'id':'a'*32,'vod_id':'123456789','meta':{'is_live':not bool(flag)},'stats':{}}
                if flag is not None:ready['meta']['captured_during_live']=flag
                message=SimpleNamespace(chat_id=5,message_id=6,reply_text=AsyncMock())
                context=SimpleNamespace(user_data={},bot=SimpleNamespace(edit_message_text=AsyncMock()))
                await _offer_ready(message,context,ready,edit=edit)
                args=context.bot.edit_message_text.await_args if edit else message.reply_text.await_args
                rows=args.kwargs['reply_markup'].inline_keyboard
                self.assertEqual(expected,rows[0][0].text)
                self.assertEqual('ui:result:open:123456789',rows[0][0].callback_data)
                self.assertEqual('Обработать чат заново',rows[1][0].text)
                nonce=context.user_data['vod_result_choice']['nonce']
                self.assertEqual('ui:result:regen:123456789:'+nonce,rows[1][0].callback_data)
                self.assertEqual('a'*32,ready['id'])


class GenerationLabels(unittest.TestCase):
    setUp=registry.ResultRegistryTestCase.setUp
    plan=registry.ResultRegistryTestCase.plan
    publish=registry.ResultRegistryTestCase.publish

    def test_label_belongs_to_the_selected_generation(self):
        from src.database.db_results import get_ready_result
        old=self.plan();old.meta['captured_during_live']=True
        self.publish(old);old.complete();old.close()
        saved=get_ready_result(old.vod_id)
        self.assertEqual(old.id,saved['id'])
        self.assertEqual(PARTIAL,ready_chat_label(saved))
        new=self.plan();new.meta['is_live']=True
        self.publish(new);new.complete();new.close()
        current=get_ready_result(new.vod_id)
        self.assertEqual(new.id,current['id'])
        self.assertEqual(NORMAL,ready_chat_label(current))
        self.assertEqual(PARTIAL,ready_chat_label(saved))
