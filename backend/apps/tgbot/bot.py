"""Telegram-бот (python-telegram-bot, long polling): команды, вопросы ассистенту, фоновая рассылка уведомлений."""
from __future__ import annotations

import asyncio
import logging

from django.conf import settings
from django.db import close_old_connections
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import services

log = logging.getLogger(__name__)
HELP = (
    "Я — ассистент сервиса прогноза денежного потока. Чат привязан к компании, выбранной на сайте при подключении; чтобы сменить её, отключите Telegram на сайте и подключите заново.\n\n"
    "/forecast — остаток, риск кассового разрыва, рекомендации\n"
    "/payments [дней] — ближайшие платежи и поступления\n"
    "/notify — о каких ситуациях присылать уведомления\n"
    "/help — эта справка\n\n"
    "Или просто напишите вопрос о финансах компании: «когда разрыв?», «что заплатить на неделе?», «что если клиент задержит оплату на 2 недели?», «как работает страница План-факт?»."
)


def _with_db(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    finally:
        close_old_connections()


async def run(fn, *args, **kwargs):
    return await asyncio.to_thread(_with_db, fn, *args, **kwargs)


async def _linked(update: Update) -> dict | None:
    info = await run(services.link_info, update.effective_chat.id)
    if not info:
        await update.message.reply_text("Чат не привязан к аккаунту. На сайте откройте окно ассистента → «Подключить Telegram» и перейдите по ссылке.")
    return info


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = ctx.args[0] if ctx.args else ""
    if code:
        info = await run(services.consume_link_code, code, update.effective_chat.id, update.effective_user.username or "")
        if not info:
            await update.message.reply_text("Код не найден или истёк. Получите новый на сайте в окне ассистента.")
            return
        await update.message.reply_text(f"Готово: аккаунт {info['user']} подключён, компания «{info['company']}». Уведомления о рисках и платежах включены.\n\n{HELP}")
        if info.get("company_id"):
            await update.message.reply_text(await run(services.forecast_text, info["company_id"]))
        return
    info = await run(services.link_info, update.effective_chat.id)
    await update.message.reply_text((f"Подключено: {info['username']}, компания «{info['company']}».\n\n" if info else "Чат пока не привязан. На сайте откройте окно ассистента → «Подключить Telegram».\n\n") + HELP)


async def cmd_help(update: Update, _ctx):
    await update.message.reply_text(HELP)


async def cmd_forecast(update: Update, _ctx):
    info = await _linked(update)
    if info:
        await update.message.chat.send_action(ChatAction.TYPING)
        await update.message.reply_text(await run(services.forecast_text, info["company_id"]))


async def cmd_payments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    info = await _linked(update)
    if info:
        days = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 7
        await update.message.reply_text(await run(services.payments_text, info["company_id"], max(1, min(days, 90))))


def _notify_keyboard(options: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(("✅ " if o["on"] else "☐ ") + o["label"], callback_data=f"notify:{o['code']}")] for o in options])


async def cmd_notify(update: Update, _ctx):
    info = await _linked(update)
    if info:
        options = await run(services.notify_options, update.effective_chat.id)
        await update.message.reply_text(f"Уведомления по компании «{info['company']}» — отметьте ситуации, о которых сообщать:", reply_markup=_notify_keyboard(options))


async def on_callback(update: Update, _ctx):
    q = update.callback_query
    kind, _, code = q.data.partition(":")
    chat_id = q.message.chat.id
    if kind == "notify":
        state = await run(services.toggle_notify, chat_id, code)
        await q.answer("Включено" if state else "Выключено" if state is False else "Недоступно")
        await q.edit_message_reply_markup(_notify_keyboard(await run(services.notify_options, chat_id)))
    else:
        await q.answer("Эта кнопка больше не поддерживается")


async def on_text(update: Update, _ctx):
    if not await _linked(update):
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    reply = await run(services.assistant_reply, update.effective_chat.id, update.message.text)
    await update.message.reply_text(reply[:4000])


async def notifier(_app: Application):
    interval = max(1, settings.TELEGRAM_NOTIFY_INTERVAL_MIN) * 60
    while True:
        try:
            sent = await run(services.send_pending_notifications)
            if sent:
                log.info("telegram: sent %s notifications", sent)
        except Exception:  # noqa: BLE001
            log.exception("telegram notifier failed")
        await asyncio.sleep(interval)


async def post_init(app: Application):
    await app.bot.set_my_commands([("forecast", "Прогноз и риск разрыва"), ("payments", "Ближайшие платежи"), ("notify", "О чём присылать уведомления"), ("help", "Справка")])
    app.bot_data["notifier"] = asyncio.create_task(notifier(app))  # ссылка в bot_data, чтобы задачу не собрал GC


def build_application() -> Application:
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("forecast", cmd_forecast))
    app.add_handler(CommandHandler("payments", cmd_payments))
    app.add_handler(CommandHandler("notify", cmd_notify))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def main() -> None:
    build_application().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
