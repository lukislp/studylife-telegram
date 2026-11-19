"""The two-step link, wired to the store: approval first, code second.

confirmation.py holds the rules, store.py the rows; this is the thin application layer that
sequences them, free of FastAPI and Telegram so the attack scenario can be tested end to end.
Nothing here attaches an account to a chat except `confirm`, and it only does so for the chat
that owns the pending confirmation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .commands import RunTracker
from .confirmation import Verdict, evaluate, new_code
from .store import LinkStore, PendingLink

KEY_REJECTED_NOTICE = (
    "StudyAtrium no longer accepts this chat's API key (it was revoked or the account was "
    "deleted), so the connection was removed. Send /login to connect again."
)


async def drop_rejected_link(store: LinkStore, runs: RunTracker, chat_id: int) -> None:
    """Removes a link whose API key the server rejected with 401. A key that has stopped working
    never works again, so keeping the row would only keep a chat id and a dead credential on file
    for nothing - and every reminder pass would keep retrying it."""
    runs.forget(chat_id)
    await store.unlink(chat_id)


@dataclass(frozen=True)
class ConfirmResult:
    verdict: Verdict | None  # None: this chat has nothing waiting for a code
    attempts_left: int = 0
    instance_url: str | None = None


async def begin_confirmation(
    store: LinkStore, pending: PendingLink, api_key: str, user_id: str | None
) -> str:
    """Parks an approved login and returns the code to show in the browser. The account is NOT
    linked yet."""
    code = new_code()
    await store.put_confirmation(pending.chat_id, code, api_key, pending.instance_url, user_id)
    return code


async def confirm(
    store: LinkStore, chat_id: int, supplied: str, now: float | None = None
) -> ConfirmResult:
    """One attempt from `chat_id`. Only that chat's own pending confirmation is ever consulted,
    which is exactly why a code that reaches the wrong chat achieves nothing."""
    now = time.time() if now is None else now
    waiting = await store.get_confirmation(chat_id)
    if waiting is None:
        return ConfirmResult(None)
    outcome = evaluate(waiting.code, supplied, waiting.attempts, waiting.created_at, now)
    if outcome.verdict is Verdict.CONFIRMED:
        # Discard first: the code is single-use even if linking were to fail half way.
        await store.discard_confirmation(chat_id)
        await store.link(chat_id, waiting.api_key, waiting.instance_url, waiting.studylife_user_id)
        return ConfirmResult(Verdict.CONFIRMED, instance_url=waiting.instance_url)
    if outcome.verdict is Verdict.WRONG:
        await store.record_wrong_attempt(chat_id)
    else:
        await store.discard_confirmation(chat_id)
    return ConfirmResult(outcome.verdict, attempts_left=outcome.attempts_left)
