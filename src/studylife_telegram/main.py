"""FastAPI app: three inbound routes, plus a health probe.

  POST /telegram           - updates from Telegram (secret token + chat allowlist)
  GET  /connect/callback   - where StudyLife returns from a /login approval
  POST /webhooks/studylife - StudyLife's outgoing webhooks (HMAC-SHA256 over the raw body)

The Telegram and StudyLife routes answer 200 for anything they choose not to act on. Telegram
retries a non-2xx and eventually disables the webhook, and StudyLife's delivery service does the
same - so "received but ignored" must never look like a failure.

Each chat acts on its OWN StudyLife account: the link is looked up per update (store.py), and no
account is configured process-wide. Alexa can stay stateless here because Amazon carries an
access token on every request; a Telegram update carries only a chat id, so the mapping has to
live on this side.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache
from html import escape
from typing import Any

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse

from .commands import ACCOUNT_FREE_COMMANDS, RunTracker, format_help, handle, parse_command
from .config import Settings
from .linking import (
    LinkError,
    build_connect_url,
    callback_url,
    exchange_assertion,
    instance_allowed,
    is_private_chat,
    new_pkce_pair,
    new_state,
    normalise_instance,
)
from .reminders import ReminderLoop
from .store import LinkedAccount, LinkStore, PendingLink
from .studylife_client import StudyLifeClient
from .telegram_client import TelegramApiError, TelegramClient
from .times import zone
from .verify import (
    is_allowed_chat,
    verify_studylife_signature,
    verify_telegram_secret,
    wants_announcement,
)

logger = logging.getLogger(__name__)

CLIENT_ID = "studylife-telegram"


class RedactCallbackQuery(logging.Filter):
    """Keeps the assertion out of uvicorn's access log.

    It arrives as a query parameter and uvicorn logs the whole request line. The assertion is
    single-use and worthless without the PKCE verifier, which never leaves this process, so this
    is hygiene rather than a hole - but a credential still has no business in a log that gets
    tailed, shipped and pasted into issues.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                a.split("?", 1)[0] + "?<redacted>"
                if isinstance(a, str) and a.startswith("/connect/callback?")
                else a
                for a in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(RedactCallbackQuery())


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if not settings.telegram_allow_any_chat and not settings.allowed_chat_ids:
        # Failing at startup rather than serving: an empty allowlist means every update would be
        # rejected, which looks like a broken bot instead of a misconfigured one. Serving
        # everybody instead would be far worse, which is why that needs its own explicit flag.
        raise RuntimeError(
            "TELEGRAM_ALLOWED_CHAT_IDS parsed to an empty set and TELEGRAM_ALLOW_ANY_CHAT is "
            "off - the bot would answer nobody."
        )
    if settings.public_base_url is None:
        # /login cannot be offered without it, and a bot whose only way in is unavailable is
        # worth failing on rather than discovering in a chat.
        raise RuntimeError("PUBLIC_BASE_URL is required: /login redirects back to it.")

    app.state.telegram = TelegramClient(settings.telegram_bot_token)
    app.state.store = LinkStore(settings.link_db_path, settings.link_encryption_key)
    await app.state.store.open()
    app.state.runs = RunTracker()
    app.state.tz = zone(settings.studylife_timezone)

    # Reminders for planned sessions. StudyLife publishes no "session is about to start" event
    # (see WebhookEventTypes - its catalogue is reactive), so the bot has to watch the clock
    # itself, exactly as the app's own client does from UserSettings.SessionReminderMinutes.
    app.state.reminders = ReminderLoop(
        accounts=app.state.store.all_links,
        fetch=_fetch_sessions,
        send=lambda chat_id, text: _send(app.state.telegram, chat_id, text),
        tz=app.state.tz,
        leads=settings.reminder_leads,
        tick_seconds=settings.reminder_tick_seconds,
        refresh_seconds=settings.reminder_refresh_seconds,
    )
    app.state.reminders.start()
    try:
        yield
    finally:
        await app.state.reminders.stop()
        await app.state.store.close()
        await app.state.telegram.aclose()


app = FastAPI(lifespan=lifespan, title="studylife-telegram")


def _client(account: LinkedAccount) -> StudyLifeClient:
    """A client for one account.

    Built per use rather than cached: a cache keyed by the API key would go on serving a key
    that /login has just rotated, and this bot's traffic is a handful of requests per minute.
    """
    return StudyLifeClient(account.instance_url, account.api_key)


async def _fetch_sessions(account: LinkedAccount) -> list[dict[str, Any]]:
    async with _client(account) as client:
        return await client.list_sessions()


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
    if chat_id is None:
        return Response(status_code=200)
    if not settings.telegram_allow_any_chat and not is_allowed_chat(
        settings.allowed_chat_ids, chat_id
    ):
        logger.warning("Ignored an update from chat %s, which is not in the allowlist", chat_id)
        return Response(status_code=200)
    if not is_private_chat(chat):
        # See linking.is_private_chat: a group's id belongs to everyone in it.
        await _send(request.app.state.telegram, chat_id, "This bot only works in a direct chat.")
        return Response(status_code=200)

    command = parse_command(message.get("text"))
    if command is None:
        return Response(status_code=200)

    reply = await _run_command(request.app, command, chat_id)
    await _send(request.app.state.telegram, chat_id, reply)
    return Response(status_code=200)


async def _run_command(app: FastAPI, command: Any, chat_id: int) -> str:
    settings = get_settings()
    store: LinkStore = app.state.store

    if command.name == "login":
        return await _start_login(store, settings, chat_id, command.argument)
    if command.name == "logout":
        app.state.runs.forget(chat_id)
        return (
            "Disconnected. The API key stays valid on StudyLife until you revoke it there."
            if await store.unlink(chat_id)
            else "This chat is not connected."
        )
    if command.name == "whoami":
        account = await store.get(chat_id)
        if account is None:
            return "Not connected. Send /login to connect your StudyLife account."
        who = f" as user {account.studylife_user_id}" if account.studylife_user_id else ""
        return f"Connected to {account.instance_url}{who}."

    if command.name in ACCOUNT_FREE_COMMANDS:
        # Only /help and /start reach here; the three linking commands are handled above.
        return format_help()

    account = await store.get(chat_id)
    if account is None:
        return "Not connected. Send /login to connect your StudyLife account."
    async with _client(account) as client:
        return await handle(
            command,
            client,
            app.state.runs,
            chat_id,
            datetime.now(app.state.tz),
            app.state.tz,
        )


async def _start_login(store: LinkStore, settings: Settings, chat_id: int, argument: str) -> str:
    instance = normalise_instance(argument or str(settings.studylife_base_url))
    if not instance_allowed(
        instance, settings.allowed_instances, settings.studylife_allow_any_instance
    ):
        return (
            f"This bot does not connect to {instance}. "
            f"Send /login on its own to use {normalise_instance(str(settings.studylife_base_url))}."
        )

    verifier, challenge = new_pkce_pair()
    state = new_state()
    await store.put_pending(
        PendingLink(state=state, chat_id=chat_id, code_verifier=verifier, instance_url=instance)
    )
    url = build_connect_url(
        instance,
        CLIENT_ID,
        callback_url(str(settings.public_base_url)),
        state,
        challenge,
    )
    # The browser hint is not decoration. Telegram's built-in browser reports WebAuthn as
    # supported but cannot reach the platform authenticator, so StudyLife's passkey sign-in
    # fails there with a generic "Anmeldung hat nicht geklappt" that names no cause. Everyone
    # using this bot would hit it exactly once and have no way to work out why.
    return (
        "Open this in your browser to approve the connection.\n\n"
        "Long-press the link and open it in Safari or Chrome - Telegram's built-in browser "
        "cannot use passkeys, and the sign-in will just fail there without saying why.\n\n"
        "The link works once and expires in 10 minutes. Do not forward it.\n\n" + url
    )


@app.get("/connect/callback")
async def connect_callback(request: Request, state: str = "", assertion: str = "") -> Response:
    """Where StudyLife sends the browser after a /login approval.

    Public by necessity - the whole point is that it works from a phone. The state is what binds
    this callback to a chat, and it is single-use; the PKCE verifier it carries never left this
    process, so a stolen assertion alone redeems nothing.

    The page says as little as possible either way: it is rendered in whatever browser the user
    opened, and the real answer is delivered to the chat.
    """
    store: LinkStore = request.app.state.store
    pending = await store.take_pending(state) if state else None
    if pending is None or not assertion:
        return _page(
            "Could not connect", "That link is expired or already used. Send /login again."
        )

    try:
        api_key, user_id = await exchange_assertion(
            pending.instance_url, CLIENT_ID, assertion, pending.code_verifier
        )
    except LinkError as exc:
        # The reason goes to the chat, which is authenticated; the page says nothing about it.
        # This endpoint is on the public internet, and even a status code echoed back here
        # tells a caller something about an instance they may have no business knowing about.
        await _send(request.app.state.telegram, pending.chat_id, str(exc))
        return _page("Could not connect", "Your chat has the details.")

    await store.link(pending.chat_id, api_key, pending.instance_url, user_id)
    await _send(
        request.app.state.telegram,
        pending.chat_id,
        f"Connected to {pending.instance_url}. Try /status or /today.",
    )
    return _page("Connected", "You can close this tab - the bot has replied in your chat.")


def _page(title: str, body: str) -> HTMLResponse:
    """A fixed, tiny page. Both arguments are constants today; they are escaped anyway so that
    adding a dynamic one later cannot quietly turn this public route into an injection point."""
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        f"<title>StudyLife</title><body style='font-family:system-ui;padding:3rem;"
        f"text-align:center'><h1>{escape(title)}</h1><p>{escape(body)}</p>"
    )


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
        # Broadcast to every connected chat. This endpoint carries no account identity - the
        # signature proves only that SOME StudyLife instance sent it - so it is only meaningful
        # in a single-account deployment. Left in place for that case; a multi-account
        # deployment should leave STUDYLIFE_WEBHOOK_SECRET unset, which disables the route.
        for account in await request.app.state.store.all_links():
            await _send(request.app.state.telegram, account.chat_id, text)
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
    if event == "course_goal.completed":
        return f"Course goal completed{suffix}."
    if event == "new_record.set":
        return f"New record - longest session so far{suffix}."
    if event == "plan.generated":
        return "A new study plan was generated."
    return ""


async def _send(telegram: TelegramClient, chat_id: int, text: str) -> None:
    """A failed send must not fail the request: Telegram would retry the whole update and the
    command would run a second time, which for a timer transition is worse than a lost reply."""
    try:
        await telegram.send_message(chat_id, text)
    except TelegramApiError as exc:
        logger.error("Could not deliver a message to chat %s: %s", chat_id, exc)
