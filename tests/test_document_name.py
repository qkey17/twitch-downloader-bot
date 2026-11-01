import asyncio
import hashlib
import json
import unittest
from src.document_name import document_name
from src.ui.ui_labels import CREATE_CHAT,RECREATE_CHAT,CHAT_GENERIC,DOWNLOAD_HTML,VOD_LINK
import test_html_sender as fixture


class NameTests(unittest.TestCase):
    def name(self,channel='channel',date='2026-11-01T18:30:00Z'):
        return document_name(json.dumps(dict(channel=channel,created_at=date)),'fallback.html')

    def test_channel_and_vod_start_in_utc(self):
        self.assertEqual('channel — 2026-11-01 18-30 UTC.html',self.name())
        self.assertEqual('Канал 日本 🎮 — 2026-11-01 18-30 UTC.html',self.name('Канал 日本 🎮'))
        self.assertEqual(self.name(),self.name(date='2026-11-01T21:30:00+03:00'))
        self.assertEqual(self.name(),self.name())
        self.assertEqual('channel — 0001-01-01 00-00 UTC.html',self.name(date='0001-01-01T00:00:00Z'))

    def test_sanitization_and_length(self):
        self.assertEqual('a b c d — 2026-11-01 18-30 UTC.html',self.name(' .. a/ b\\c\n\x00:*?"<>|\u202ed . '))
        value=self.name('я🎮'*1000)
        self.assertLessEqual(len(value.encode('utf-8')),200)
        self.assertTrue(value.endswith(' UTC.html'))
        self.assertEqual(self.name('e\u0301'),self.name('é'))

    def test_invalid_or_missing_values_use_existing_name(self):
        for date in (None,'','2026-02-30T01:00:00Z','2026-01-01','2026-01-01T01:00:00',{},'not a date'):
            self.assertEqual('fallback.html',self.name(date=date))
        for channel in (' .. ',None,{},'\x00/\\'):
            self.assertEqual('fallback.html',self.name(channel))
        for raw in ('bad','[]','null',None,'x'*65537):
            self.assertEqual('old.html',document_name(raw,'old.html'))

    def test_exact_ui_labels(self):
        self.assertEqual(('Обработать чат','Обработать чат заново','🌐 Посмотреть чат онлайн','📥 Скачать чат файлом','🔗 Ссылка на VOD'),
                         (CREATE_CHAT,RECREATE_CHAT,CHAT_GENERIC,DOWNLOAD_HTML,VOD_LINK))


class DocumentDeliveryTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.HtmlSenderTests.setUp
    asyncTearDown=fixture.HtmlSenderTests.asyncTearDown
    plan=fixture.HtmlSenderTests.plan
    publish=fixture.HtmlSenderTests.publish
    rows=fixture.HtmlSenderTests.rows
    def _setupAsyncioRunner(self):
        self._asyncioRunner=asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)

    async def test_repeated_delivery_renames_only_multipart_filename(self):
        from src.html_sender import SendStatus
        from src.database import db_core
        plan=self.plan();plan.meta.update(channel='Канал 🎮',created_at='2026-11-01T18:30:00Z')
        self.publish(plan);plan.complete();plan.close()
        path=plan.path();before=hashlib.sha256(path.read_bytes()).hexdigest()
        rows_before=self.rows();names=set(path.parent.iterdir())
        for _ in range(2):
            r=await self.service.send(7,1,plan.vod_id,generation_id=plan.id)
            self.assertEqual(SendStatus.SENT,r.status)
            document=self.bot.send_document.await_args.kwargs['document']
            self.assertEqual('Канал 🎮 — 2026-11-01 18-30 UTC.html',document.filename)
            self.assertFalse(isinstance(document.input_file_content,bytes))
        self.assertEqual(before,hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(names,set(path.parent.iterdir()))
        self.assertEqual(rows_before,self.rows())
