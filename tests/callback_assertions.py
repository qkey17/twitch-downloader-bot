from src.ui.ui_labels import STALE_ACTION_NOTICE, STALE_LIST_NOTICE


def assert_stale(test, update):
    q = update.callback_query
    if update.effective_user.id == 7 and (q.data or '').startswith('ui:vl:'):
        q.answer.assert_awaited_once_with(STALE_LIST_NOTICE, show_alert=True)
    else:
        q.answer.assert_awaited_once_with(STALE_ACTION_NOTICE)
