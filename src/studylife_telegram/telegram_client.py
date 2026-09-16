"""Minimal Telegram Bot API client - only the four calls this bot makes.

The bot token sits in the URL path rather than a header, which is Telegram's design, not a
choice here. It makes every request URL a credential: never log one, and use `redact` on
anything that might end up in an error message.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

API_ROOT = "https://api.telegram.org"

_TOKEN_IN_URL = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+")


def redact(text: str) -> str:
    """Replaces a bot token wherever it appears in a URL. httpx puts the full URL into its
    exception messages, so an unredacted error log would leak the token."""
    return _TOKEN_IN_URL.sub("/bot<redacted>", text)


class TelegramApiError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Telegram API returned {status_code}: {redact(body.strip())[:200]}")
        self.status_code = status_code


class TelegramClient:
    def __init__(self, bot_token: str, timeout: float = 10.0) -> None:
        self._http = httpx.AsyncClient(base_url=f"{API_ROOT}/bot{bot_token}", timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(f"/{method}", json=payload)
        except httpx.HTTPError as exc:  # message can contain the URL, and the URL has the token
            raise TelegramApiError(0, redact(str(exc))) from None
        if response.status_code >= 400:
            raise TelegramApiError(response.status_code, response.text)
        return dict(response.json())

    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        # No parse_mode: replies carry course names and note text the user typed, and running
        # those through Markdown/HTML parsing means an unbalanced "*" or "<" from real content
        # makes Telegram reject the whole message. Plain text always renders.
        return await self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )

    async def set_webhook(self, url: str, secret_token: str) -> dict[str, Any]:
        return await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                # Only messages are handled; not subscribing to the rest keeps Telegram from
                # delivering edits, reactions and channel posts this bot would only discard.
                "allowed_updates": ["message"],
                "drop_pending_updates": True,
            },
        )

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> dict[str, Any]:
        """Registers the command list Telegram shows in the "/" menu."""
        return await self._call(
            "setMyCommands",
            {"commands": [{"command": c, "description": d} for c, d in commands]},
        )
