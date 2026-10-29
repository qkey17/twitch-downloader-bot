import asyncio
import json
import unittest
from unittest.mock import patch

import test_disk_pipeline as fixture
import test_channel_selection as startup_fixture
from src import resource_usage as usage
from src.workers import worker_html
from src.twitch_api.client import TwitchAPIError


class TelemetryPipelineTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.DiskPipelineTestCase.setUp
    source = fixture.DiskPipelineTestCase.source
    run_chat = fixture.DiskPipelineTestCase.run_chat
    assert_clean = fixture.DiskPipelineTestCase.assert_clean

    def events(self, captured):
        return [json.loads(record.getMessage()) for record in captured.records]

    async def test_success_correlates_stages_metrics_and_one_final_event(self):
        with self.assertLogs('tcd.resources', 'INFO') as captured:
            await self.run_chat(self.source(10))
        rows = self.events(captured)
        self.assertEqual(1, len({r['operation_id'] for r in rows}))
        self.assertRegex(rows[0]['operation_id'], '^[0-9a-f]{32}$')
        self.assertEqual({'result_reserve', 'vod_metadata', 'messages_spool', 'emotes_badges',
                          'analysis_html_publication', 'result_commit'},
                         {r['stage'] for r in rows if r['event'] == 'stage_end'})
        self.assertEqual(8, len(rows))
        final = rows[-1]
        self.assertEqual(('operation_end', 'success'), (final['event'], final['outcome']))
        self.assertEqual(10, final['metrics']['messages'])
        self.assertGreater(final['metrics']['spool_bytes'], 0)
        self.assertGreater(final['metrics']['html_bytes'], 0)
        self.assertTrue(all(r['resource_scope'] == 'process' for r in rows))
        self.assertTrue(all(r['elapsed_seconds'] >= 0 for r in rows if r['event'] != 'operation_start'))
        self.assert_clean()

    async def test_fetch_error_is_safe_and_has_exactly_one_failure_summary(self):
        async def broken():
            async for comment in self.source(1):
                yield comment
            raise TwitchAPIError('SECRET_TOKEN User chat contents')
        with self.assertLogs('tcd.resources', 'INFO') as captured:
            await self.run_chat(broken())
        rows = self.events(captured)
        self.assertEqual(1, sum(r['event'] == 'operation_end' for r in rows))
        self.assertEqual(('error', 'operation_error'), (rows[-1]['outcome'], rows[-1]['error_code']))
        self.assertNotIn('SECRET_TOKEN', str(rows))
        self.assertNotIn('chat contents', str(rows))
        self.assertNotIn('chat_id', str(rows))
        self.assert_clean()

    async def test_html_error_is_classified_without_raw_exception(self):
        with patch.object(worker_html, 'write_viewer_html', side_effect=ValueError('SECRET_HTML')):
            with self.assertLogs('tcd.resources', 'INFO') as captured:
                await self.run_chat(self.source(3))
        rows = self.events(captured)
        self.assertEqual('invalid_data', rows[-1]['error_code'])
        self.assertNotIn('SECRET_HTML', str(rows))
        self.assertEqual('error', rows[-1]['outcome'])
        self.assert_clean()

    async def test_cooperative_and_task_cancellation_have_cancelled_outcomes(self):
        for error in (RuntimeError('Загрузка была отменена.'), asyncio.CancelledError()):
            async def cancelled():
                yield (1, '', 'private_user', 'private text', [], '', [])
                raise error
            with self.assertLogs('tcd.resources', 'INFO') as captured:
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.run_chat(cancelled())
                else:
                    await self.run_chat(cancelled())
            rows = self.events(captured)
            self.assertEqual('cancelled', rows[-1]['outcome'])
            self.assertEqual(1, sum(r['event'] == 'operation_end' for r in rows))
            self.assertNotIn('private_user', str(rows))
            self.assertNotIn('private text', str(rows))
            self.assert_clean()

    async def test_missing_metrics_do_not_prevent_success(self):
        with patch.object(usage, 'memory_usage', side_effect=OSError('PRIVATE_PATH')):
            with self.assertLogs('tcd.resources', 'INFO') as captured:
                await self.run_chat(self.source(3))
        rows = self.events(captured)
        self.assertIsNone(rows[-1]['process_memory']['rss_bytes'])
        self.assertEqual('success', rows[-1]['outcome'])

    async def test_logging_failure_does_not_affect_operation(self):
        with patch.object(usage.logging.getLogger('tcd.resources'), 'info', side_effect=RuntimeError('sink failed')):
            await self.run_chat(self.source(3))
        self.assert_clean()

    async def test_many_batches_do_not_add_unbounded_events(self):
        with self.assertLogs('tcd.resources', 'INFO') as captured:
            await self.run_chat(self.source(100))
        self.assertEqual(8, len(self.events(captured)))


class TelemetryUnitTests(unittest.TestCase):
    def test_virtual_monotonic_durations_and_duplicate_finish(self):
        with patch.object(usage.OperationTelemetry, 'clock', side_effect=(100, 101, 104, 110)):
            operation = usage.OperationTelemetry('a'*32)
            token = usage._CURRENT.set(operation)
            try:
                with self.assertLogs('tcd.resources', 'INFO') as captured:
                    with usage.stage('synthetic'):
                        pass
                    operation.finish()
                    operation.finish()
            finally:
                usage._CURRENT.reset(token)
        rows = [json.loads(r.getMessage()) for r in captured.records]
        self.assertEqual([3, 10], [r['elapsed_seconds'] for r in rows])
        self.assertEqual(1, sum(r['event'] == 'operation_end' for r in rows))

    def test_metrics_allowlist_and_untrusted_id_are_not_logged(self):
        operation = usage.OperationTelemetry('secret-user-id')
        operation.update({'messages': 10, 'token': 'SECRET', 'chat_id': 123, 'spool_bytes': -1})
        with self.assertLogs('tcd.resources', 'INFO') as captured:
            operation.finish()
        row = json.loads(captured.records[0].getMessage())
        self.assertEqual({'messages': 10}, row['metrics'])
        self.assertIsNone(row['operation_id'])
        self.assertNotIn('SECRET', str(row))

    def test_clock_and_cpu_failure_leave_optional_values(self):
        with patch.object(usage.time, 'monotonic', side_effect=OSError()), patch.object(usage.time, 'process_time', side_effect=OSError()):
            operation = usage.OperationTelemetry('b'*32)
            with self.assertLogs('tcd.resources', 'INFO') as captured:
                operation.finish()
        row = json.loads(captured.records[0].getMessage())
        self.assertIsNone(row['elapsed_seconds'])
        self.assertIsNone(row['process_cpu_seconds'])


class StartupTelemetryTests(unittest.IsolatedAsyncioTestCase):
    _setupAsyncioRunner = startup_fixture.ChannelSelectionTests._setupAsyncioRunner
    setUp = startup_fixture.ChannelSelectionTests.setUp
    asyncTearDown = startup_fixture.ChannelSelectionTests.asyncTearDown
    task = startup_fixture.ChannelSelectionTests.task
    message = startup_fixture.ChannelSelectionTests.message
    update = startup_fixture.ChannelSelectionTests.update
    context_for_user = startup_fixture.ChannelSelectionTests.context_for_user

    async def test_task_creation_failure_emits_safe_final_event_and_releases_slot(self):
        from src.handlers import handlers_vod as vod
        self.app.create_task.side_effect = RuntimeError('PRIVATE_STARTUP_FAILURE')
        with self.assertLogs('tcd.resources', 'INFO') as captured:
            with self.assertRaises(RuntimeError):
                await vod._start_download(self.update(''), self.context, '123', modern=True)
        rows = [json.loads(r.getMessage()) for r in captured.records]
        self.assertEqual(['operation_start', 'operation_end'], [r['event'] for r in rows])
        self.assertEqual('error', rows[-1]['outcome'])
        self.assertNotIn('PRIVATE_STARTUP_FAILURE', str(rows))
        self.assertNotIn('vod_global_operation', self.app.bot_data)

    async def test_cancel_before_runner_first_turn_still_emits_one_summary(self):
        from src.handlers import handlers_vod as vod
        def cancelled(coroutine):
            task = self.task(coroutine)
            task.cancel()
            return task
        self.app.create_task.side_effect = cancelled
        with self.assertLogs('tcd.resources', 'INFO') as captured:
            await vod._start_download(self.update(''), self.context, '123', modern=True)
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await asyncio.sleep(0)
        rows = [json.loads(r.getMessage()) for r in captured.records]
        self.assertEqual(1, sum(r['event'] == 'operation_end' for r in rows))
        self.assertEqual('cancelled', rows[-1]['outcome'])
        self.assertNotIn('vod_global_operation', self.app.bot_data)
