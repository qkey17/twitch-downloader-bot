import asyncio
import io
import logging
import os
from pathlib import Path
import traceback
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

from telegram.error import InvalidToken, NetworkError, TimedOut
from telegram.ext import Application
from telegram.request import HTTPXRequest
from telegram._utils.files import parse_file_input

from src import config, main, telegram_api

TOKEN = '123456789:fake-token-for-api-mode-tests'


class ModeTests(unittest.TestCase):
    def test_unset_and_explicit_cloud(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual('cloud', config.get_telegram_api_mode())
            os.environ['TCD_TELEGRAM_API_MODE'] = 'cloud'
            self.assertEqual('cloud', config.get_telegram_api_mode())

    def test_explicit_local(self):
        with patch.dict(os.environ, {'TCD_TELEGRAM_API_MODE': 'local'}, clear=True):
            self.assertEqual('local', config.get_telegram_api_mode())

    def test_invalid_values_are_not_echoed(self):
        for value in ('', 'LOCAL', ' cloud ', 'auto', 'http://localhost:8081/bot'+TOKEN):
            with self.subTest(value=value), patch.dict(os.environ, {'TCD_TELEGRAM_API_MODE': value}, clear=True):
                with self.assertRaises(config.ConfigurationError) as error:
                    config.get_telegram_api_mode()
                self.assertEqual("TCD_TELEGRAM_API_MODE must be 'cloud' or 'local'", str(error.exception))
                self.assertNotIn(TOKEN, str(error.exception))

    def test_invalid_mode_stops_main_before_application_or_credentials(self):
        with patch.dict(os.environ, {'TCD_TELEGRAM_API_MODE': TOKEN}, clear=True), \
             patch.object(main, 'setup_logging'), patch.object(main, 'get_telegram_bot_token') as token, \
             patch.object(main.Application, 'builder') as builder, self.assertLogs('tcd', 'ERROR') as logs:
            with self.assertRaises(config.ConfigurationError):
                main.main()
        token.assert_not_called(); builder.assert_not_called()
        self.assertNotIn(TOKEN, '\n'.join(logs.output))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def build(self, mode):
        with patch.object(HTTPXRequest, 'do_request', new=AsyncMock(side_effect=AssertionError('No network'))) as network:
            app = telegram_api.build_application(Application.builder().token(TOKEN), mode, TOKEN)
        network.assert_not_awaited()
        for request in app.bot._request:
            self.addAsyncCleanup(request.shutdown)
        return app

    async def test_cloud_retains_real_ptb_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            app = await self.build(config.get_telegram_api_mode())
        self.assertEqual('https://api.telegram.org/bot'+TOKEN, app.bot.base_url)
        self.assertEqual('https://api.telegram.org/file/bot'+TOKEN, app.bot.base_file_url)
        self.assertFalse(app.bot.local_mode)

    async def test_explicit_cloud_matches_default_client(self):
        app = await self.build('cloud')
        self.assertFalse(app.bot.local_mode)
        self.assertEqual('https://api.telegram.org/bot'+TOKEN, app.bot.base_url)

    async def test_local_urls_append_token_once(self):
        app = await self.build('local')
        self.assertTrue(app.bot.local_mode)
        self.assertEqual('http://127.0.0.1:8081/bot'+TOKEN, app.bot.base_url)
        self.assertEqual('http://127.0.0.1:8081/file/bot'+TOKEN, app.bot.base_file_url)
        self.assertEqual(1, app.bot.base_url.count(TOKEN))
        self.assertEqual(1, app.bot.base_file_url.count(TOKEN))

    async def test_http_clients_and_timeouts_unchanged_in_both_modes(self):
        for mode in ('cloud', 'local'):
            app = await self.build(mode)
            self.assertIs(app.updater.bot, app.bot)
            self.assertEqual(2, len(app.bot._request))  # PTB's existing polling/API clients.
            for request in app.bot._request:
                self.assertIsInstance(request, HTTPXRequest)
                timeout = request._client.timeout
                self.assertEqual((5,5,5,1), (timeout.connect,timeout.read,timeout.write,timeout.pool))
                self.assertEqual(20, request._media_write_timeout)

    async def test_local_mode_supports_path_uri_without_reading_file(self):
        app = await self.build('local')
        path = Path('future-result.html').absolute()
        with patch('telegram._utils.files.is_local_file', return_value=True), \
             patch.object(Path, 'open', side_effect=AssertionError('Must not read file')):
            self.assertEqual(path.as_uri(), parse_file_input(path, local_mode=app.bot.local_mode))


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.formatter = logging.Formatter('%(levelname)s %(message)s')
        self.handler.setFormatter(self.formatter)
        logging.getLogger().addHandler(self.handler)
        self.addCleanup(logging.getLogger().removeHandler, self.handler)

    def test_one_poller_and_identical_arguments(self):
        for mode in ('cloud', 'local'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'TCD_TELEGRAM_API_MODE': mode}, clear=True), \
                 patch.object(main, 'get_telegram_bot_token', return_value=TOKEN), \
                 patch.object(main, 'validate_credentials'), patch.object(main, 'setup_logging'), \
                 patch.object(main.Application, 'builder') as factory:
                builder = factory.return_value
                for method in ('token','post_init','post_stop','post_shutdown','base_url','base_file_url','local_mode'):
                    getattr(builder, method).return_value = builder
                app = builder.build.return_value
                main.main()
                factory.assert_called_once_with(); builder.build.assert_called_once_with()
                app.run_polling.assert_called_once_with(drop_pending_updates=True)
                builder.post_init.assert_called_once_with(main.post_init)
                builder.post_stop.assert_called_once_with(main.post_stop)
                builder.post_shutdown.assert_called_once_with(main.post_shutdown)
                app.add_handler.assert_called()
                if mode == 'cloud':
                    builder.base_url.assert_not_called(); builder.base_file_url.assert_not_called()
                    builder.local_mode.assert_not_called()
                else:
                    builder.base_url.assert_called_once_with(telegram_api.LOCAL_BASE_URL)
                    builder.base_file_url.assert_called_once_with(telegram_api.LOCAL_FILE_URL)
                    builder.local_mode.assert_called_once_with(True)

    def test_local_failure_is_sanitized_and_never_retries_cloud(self):
        for error_type in (NetworkError, TimedOut, InvalidToken):
            app = Mock()
            url = telegram_api.LOCAL_BASE_URL + TOKEN
            def fail(**kwargs):
                try:
                    raise error_type(url)
                except error_type:
                    logging.getLogger('telegram.ext').error('Startup at %s', url, exc_info=True)
                    raise
            app.run_polling.side_effect = fail
            with self.subTest(error=error_type):
                with self.assertRaises(config.ConfigurationError) as caught:
                    telegram_api.run_polling(app, 'local', TOKEN)
                diagnostic = ''.join(traceback.format_exception(caught.exception))
                self.assertNotIn(TOKEN, diagnostic)
                self.assertIn('No cloud fallback', diagnostic)
                app.run_polling.assert_called_once_with(drop_pending_updates=True)
                self.assertIs(self.handler.formatter, self.formatter)
        self.assertNotIn(TOKEN, self.stream.getvalue())
        self.assertIn('[redacted]', self.stream.getvalue())

    def test_local_build_debug_logs_and_invalid_token_are_sanitized(self):
        logger = logging.getLogger('telegram')
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, old_level)
        builder = Mock()
        builder.base_url.return_value = builder
        builder.base_file_url.return_value = builder
        builder.local_mode.return_value = builder
        def fail():
            logger.debug('URL %s or encoded %s', TOKEN, quote(TOKEN, safe=''))
            raise InvalidToken(TOKEN)
        builder.build.side_effect = fail
        with self.assertRaises(config.ConfigurationError) as caught:
            telegram_api.build_application(builder, 'local', TOKEN)
        self.assertNotIn(TOKEN, ''.join(traceback.format_exception(caught.exception)))
        self.assertNotIn(TOKEN, self.stream.getvalue())
        self.assertNotIn(quote(TOKEN, safe=''), self.stream.getvalue())
        self.assertIs(self.handler.formatter, self.formatter)

    def test_cloud_does_not_intercept_errors_or_change_formatters(self):
        app = Mock()
        error = NetworkError('cloud unavailable')
        app.run_polling.side_effect = error
        with self.assertRaises(NetworkError) as caught:
            telegram_api.run_polling(app, 'cloud', TOKEN)
        self.assertIs(error, caught.exception)
        self.assertIs(self.handler.formatter, self.formatter)
        app.run_polling.assert_called_once_with(drop_pending_updates=True)

    def test_real_ptb_local_bootstrap_failure_does_not_poll_or_fallback(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        apps = []
        original_build = telegram_api.build_application
        def capture(*args):
            app = original_build(*args)
            apps.append(app)
            return app
        request = AsyncMock(side_effect=NetworkError('Unreachable '+TOKEN))
        logger = logging.getLogger('telegram')
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            with patch.dict(os.environ, {'TCD_TELEGRAM_API_MODE':'local'}, clear=True), \
                 patch.object(main, 'get_telegram_bot_token', return_value=TOKEN), \
                 patch.object(main, 'validate_credentials'), patch.object(main, 'setup_logging'), \
                 patch.object(main, 'post_init', new=AsyncMock()) as initialized, \
                 patch.object(main, 'post_shutdown', new=AsyncMock()), \
                 patch.object(main, 'build_application', side_effect=capture), \
                 patch.object(HTTPXRequest, 'do_request', new=request):
                with self.assertRaises(config.ConfigurationError) as caught:
                    main.main()
            self.assertEqual(1, len(apps))
            request.assert_awaited_once()
            self.assertEqual(telegram_api.LOCAL_BASE_URL+TOKEN+'/getMe', request.call_args.kwargs['url'])
            self.assertFalse(apps[0].updater.running)
            initialized.assert_not_awaited()
            self.assertNotIn(TOKEN, ''.join(traceback.format_exception(caught.exception)))
            self.assertNotIn(TOKEN, self.stream.getvalue())
        finally:
            logger.setLevel(old_level)
            if not loop.is_closed(): loop.close()
            async def cleanup():
                for app in apps:
                    for client in app.bot._request:
                        await client.shutdown()
            asyncio.run(cleanup())
            asyncio.set_event_loop(None)

    def test_unvalidated_mode_cannot_silently_enable_local(self):
        builder, app = Mock(), Mock()
        with self.assertRaises(config.ConfigurationError):
            telegram_api.build_application(builder, 'auto', TOKEN)
        with self.assertRaises(config.ConfigurationError):
            telegram_api.run_polling(app, 'auto', TOKEN)
        builder.build.assert_not_called(); builder.base_url.assert_not_called()
        app.run_polling.assert_not_called()
