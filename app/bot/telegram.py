"""The Telegram bot: a pipe between the owner and the agent.

The bot does not think. It takes a message, hands it to the model with access to the
domain, and returns the answer. The conversation is with the same agent as in a terminal,
just through another door.

Other chats are ignored: the client is identified by the owner's chat_id, and no token
ever takes part in the conversation.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.bot.agent import ToolContext, answer
from app.config import get_settings
from app.core.security import AuthError, authenticate_telegram
from app.db import session_scope

log = logging.getLogger(__name__)

MAX_HISTORY = 20  # turns; older ones are dropped


def _trim(history: list[dict]) -> list[dict]:
    """Cut the history without splitting a tool call from its result.

    A `tool_result` is only valid immediately after the `tool_use` that produced it. A
    blind slice can land between the two, leaving an orphan first message — and the API
    rejects the whole request with BadRequestError. The user sees the bot die mid-answer
    for no visible reason, which is exactly what happened.
    """
    trimmed = history[-MAX_HISTORY:]
    while trimmed:
        content = trimmed[0].get("content")
        orphan = (
            isinstance(content, list)
            and content
            and getattr(content[0], "type", None) == "tool_result"
            or isinstance(content, list)
            and content
            and isinstance(content[0], dict)
            and content[0].get("type") == "tool_result"
        )
        if not orphan:
            break
        trimmed = trimmed[1:]
    return trimmed


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    chat_id = update.effective_chat.id
    with session_scope() as session:
        try:
            identity = authenticate_telegram(session, chat_id)
        except AuthError:
            log.warning("Message from an unlinked chat %s — ignoring", chat_id)
            await message.reply_text(
                "This chat is not linked to the owner. Nothing will happen."
            )
            return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    history = context.chat_data.setdefault("history", [])

    # The database session is open while the tools run. Waiting on the model happens
    # inside, but each tool is a short transaction: that is a correctness condition
    # for SQLite and must not be broken.
    with session_scope() as session:
        ctx = ToolContext(session=session, identity=identity)
        try:
            text, new_history = answer(ctx, history, message.text)
        except Exception as exc:  # noqa: BLE001 — a failure must not kill the bot
            log.exception("Failed while handling a message")
            await message.reply_text(
                f"Broke: {type(exc).__name__}. Tell the owner — I cannot fix myself."
            )
            return

    context.chat_data["history"] = _trim(new_history)
    await message.reply_text(text, disable_web_page_preview=True)


async def decide_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The two buttons under a request to connect a phone.

    This is the whole login screen. There is no password anywhere in the system, so the
    right to grant access belongs to whoever holds this chat — which is why the chat is
    checked first, before the button is honoured.
    """
    from app.core import oauth

    query = update.callback_query
    if query is None or not query.data:
        return
    await query.answer()

    chat_id = update.effective_chat.id if update.effective_chat else None
    with session_scope() as session:
        try:
            authenticate_telegram(session, chat_id)
        except AuthError:
            log.warning("An approval button was pressed from an unlinked chat %s", chat_id)
            return

    _, action, request_id = query.data.split(":", 2)
    with session_scope() as session:
        try:
            if action == "ok":
                oauth.mark_approved(session, request_id)
                verdict = "✅ Доступ разрешён. Возвращайся в приложение — оно уже подключается."
            else:
                oauth.deny(session, request_id)
                verdict = "⛔️ Отклонено. Доступ не выдан."
        except oauth.OAuthError as exc:
            verdict = f"Не вышло: {exc.description or exc.error}"

    await query.edit_message_text(verdict)


def build_application() -> Application:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CallbackQueryHandler(decide_access, pattern=r"^oauth:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    return application


def _silence_token_logging() -> None:
    """httpx logs the full request URL, and Telegram puts the token IN the URL.

    At INFO level that means the token in the container log, in `docker logs` output,
    and anywhere that output is pasted. The bot key leaks not through configuration
    but through a debug log — so this network noise is silenced explicitly.
    """
    for name in ("httpx", "httpcore", "telegram.ext.Updater"):
        logging.getLogger(name).setLevel(logging.WARNING)


def run() -> None:
    logging.basicConfig(level=get_settings().log_level)
    _silence_token_logging()
    log.info("Bot starting")
    build_application().run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    run()
