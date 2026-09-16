"""FastAPI app: two inbound routes, both authenticated, plus a health probe.

  POST /telegram           - updates from Telegram (secret token + chat allowlist)
  POST /webhooks/studylife - StudyLife's outgoing webhooks (HMAC-SHA256 over the raw body)

Both answer 200 for anything they choose not to act on. Telegram retries a non-2xx and
eventually disables the webhook, and StudyLife's delivery service does the same - so "received
but ignored" must never look like a failure.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, Header, Request, Response

from .commands import handle, parse_command
from .config import Settings
from .studylife_client import StudyLifeClient
from .telegram_client import TelegramApiError, TelegramClient
from .verify import (
    is_allowed_chat,
    verify_studylife_signature,
    verify_telegram_secret,
    wants_announcement,
)

logger = logging.getLogger(__name__)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if not settings.allowed_chat_ids:
        # Failing at startup rather than serving: an empty allowlist means every command would
        # be rejected, which looks like a broken bot instead of a misconfigured one.
        raise RuntimeError(
            "TELEGRAM_ALLOWED_CHAT_IDS parsed to an empty set - the bot would answer nobody."
        )
    app.state.studylife = StudyLifeClient(
        str(settings.studylife_base_url), settings.studylife_api_key
    )
    app.state.telegram = TelegramClient(settings.telegram_bot_token)
    try:
        yield
    finally:
        await app.state.studylife.aclose()
        await app.state.telegram.aclose()


app = FastAPI(lifespan=lifespan, title="studylife-telegram")


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/telegram")
async def telegram_update(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    if not verify_telegram_secret(
        settings.telegram_webhook_secret, x_telegram_bot_api_secret_token
    ):
        # 403 and nothing else. Telegram only ever sends the right token, so a wrong one is
        # somebody else probing a public URL; they learn nothing from this.
        return Response(status_code=403)

    update: dict[str, Any] = await request.json()
    message = update.get("message")
    if not isinstance(message, dict):
        return Response(status_code=200)

    chat = message.get("chat")
    raw_chat_id = chat.get("id") if isinstance(chat, dict) else None
    # Narrowed to int here rather than at the call site, so the allowlist check and the reply
    # provably talk about the same value.
    chat_id = raw_chat_id if isinstance(raw_chat_id, int) else None
    if chat_id is None or not is_allowed_chat(settings.allowed_chat_ids, chat_id):
        logger.warning("Ignored an update from chat %s, which is not in the allowlist", chat_id)
        return Response(status_code=200)

    command = parse_command(message.get("text"))
    if command is None:
        return Response(status_code=200)

    reply = await handle(command, request.app.state.studylife)
    await _send(request.app.state.telegram, chat_id, reply)
    return Response(status_code=200)


@app.post("/webhooks/studylife")
async def studylife_webhook(
    request: Request,
    x_studylife_webhook_signature: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    if not settings.studylife_webhook_secret:
        return Response(status_code=404)

    # The RAW body: re-serialising the parsed JSON reorders keys and the HMAC would never match.
    body = await request.body()
    if not verify_studylife_signature(
        settings.studylife_webhook_secret, x_studylife_webhook_signature, body
    ):
        return Response(status_code=403)

    payload: dict[str, Any] = await request.json()
    if not wants_announcement(payload.get("eventType")):
        return Response(status_code=200)

    text = _announcement_text(payload)
    if text:
        for chat_id in sorted(settings.allowed_chat_ids):
            await _send(request.app.state.telegram, chat_id, text)
    return Response(status_code=200)


def _announcement_text(payload: dict[str, Any]) -> str:
    event = payload.get("eventType")
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    course = data.get("courseName")
    suffix = f" - {course}" if isinstance(course, str) and course else ""
    if event == "timer.started":
        return f"Focus session started{suffix}."
    if event == "timer.ended":
        return f"Focus session ended{suffix}."
    if event == "session.completed":
        return f"Session logged{suffix}."
    if event == "goal.due":
        return f"Course goal due{suffix}."
    return ""


async def _send(telegram: TelegramClient, chat_id: int, text: str) -> None:
    """A failed send must not fail the request: Telegram would retry the whole update and the
    command would run a second time, which for a timer transition is worse than a lost reply."""
    try:
        await telegram.send_message(chat_id, text)
    except TelegramApiError as exc:
        logger.error("Could not deliver a message to chat %s: %s", chat_id, exc)
