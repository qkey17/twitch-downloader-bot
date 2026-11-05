from telegram import Update, LinkPreviewOptions
from telegram.ext import ContextTypes
from src.ui.ui import about_text, build_about_keyboard
from .handlers_state import is_busy, get_pending, clear_pending
from .handlers_input import cancel_input_request


_HELP_TEXT = (
    "Отправьте имя канала или ссылку на него, чтобы посмотреть список доступных для чтения трансляций.\n\n"
    "Либо отправьте ID или ссылку на необходимую трансляцию.\n\n"
    "Тип запроса определяется автоматически. При необходимости укажите его явно, отправив одну из команд вместе со значением:\n\n"
    "«/vod значение» — получить чат VOD.\n"
    "«/channel значение» — посмотреть VODs канала."
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_command(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        _HELP_TEXT,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await msg.reply_text(
        about_text(),
        parse_mode="HTML",
        reply_markup=build_about_keyboard(),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio
    from src.admin_batch import stop_batch, KEY
    from src.vod_cancellation import request_owner, finish_owner, invalidate_inputs
    owner = update.effective_user.id
    msg = update.effective_message
    tasks = context.application.bot_data.setdefault('tdb_owner_cancellations', {})
    existing = tasks.get(owner)
    if existing is not None:
        clean = await asyncio.shield(existing)
        await msg.reply_text('Ваши текущие и ожидающие обработки отменены' if clean else 'Не удалось подтвердить штатное завершение отмены')
        return
    batch = context.application.bot_data.get(KEY)
    batch_owned = batch is not None and batch.owner == owner
    batch_task = stop_batch(context.application, owner)  # Fence issuance first.
    plan = request_owner(context.application, owner)
    inputs = invalidate_inputs(context, owner)
    operation = context.user_data.get('vod_active_operation')
    manager = context.application.bot_data.get('tdb_vod_scheduler')
    legacy = (operation is not None and (manager is None or operation not in manager.jobs)
              and context.application.bot_data.get('vod_global_operation') == operation)
    if legacy:
        from .handlers_state import set_cancel
        set_cancel(context, True)
    has_work = bool(plan[1] or plan[2] or batch_owned or legacy)
    if not has_work:
        await msg.reply_text('Действие отменено.' if inputs else 'Нет активных обработок')
        return
    async def finish():
        try:
            clean = await finish_owner(plan)
            while legacy and context.user_data.get('vod_active_operation') == operation:
                await asyncio.sleep(.05)
            if batch_task is not None:
                await asyncio.gather(batch_task, return_exceptions=True)
            return clean and not (batch_owned and batch.cleanup_failed)
        finally:
            tasks.pop(owner, None)
    task = asyncio.create_task(finish())
    tasks[owner] = task
    await msg.reply_text('Отмена обработок…' if not plan[2] else 'Обработка уже завершается. Ожидайте завершения.')
    clean = await asyncio.shield(task)
    if not clean:
        await msg.reply_text('Не удалось подтвердить штатное завершение отмены')
        return
    await msg.reply_text('Ваши текущие и ожидающие обработки отменены' if plan[1] or batch_owned or legacy
                         else 'Нет активных обработок. Завершённые результаты сохранены')
