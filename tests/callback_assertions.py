from src.ui.ui_labels import STALE_ACTION_NOTICE


def assert_stale(test, update):
    update.callback_query.answer.assert_awaited_once_with(STALE_ACTION_NOTICE, show_alert=False)
