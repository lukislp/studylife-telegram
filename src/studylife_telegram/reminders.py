"""Proactive reminders for planned study sessions.

StudyLife itself has no webhook event for "a session is about to start" - its event catalogue
(WebhookEventTypes) is entirely reactive: session.created fires when a session is PLANNED, not
when it comes due. The app's own reminders are computed client-side from
UserSettings.SessionReminderMinutes. This module does the same thing for Telegram: it polls the
planned sessions and fires as each lead time is crossed.

Split into a pure part (which reminders are due) and a loop (fetching, sending), so the part
that is easy to get wrong - firing twice, firing for a session that already started, firing all
seven leads at once after a restart - is testable without a clock or a network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, tzinfo
from typing import Any, NamedTuple

from .times import parse_local

logger = logging.getLogger(__name__)


class Reminder(NamedTuple):
    session_id: int
    minutes_before: int
    text: str


def format_reminder(session: dict[str, Any], minutes: int, start: datetime) -> str:
    course = session.get("courseName")
    course = course if isinstance(course, str) and course else "Study session"
    topic = session.get("topic")
    suffix = f" - {topic}" if isinstance(topic, str) and topic.strip() else ""
    when = start.strftime("%H:%M")
    if minutes <= 0:
        return f"Starting now: {course} at {when}{suffix}"
    unit = "minute" if minutes == 1 else "minutes"
    return f"In {minutes} {unit}: {course} at {when}{suffix}"


def due_reminders(
    sessions: list[dict[str, Any]],
    now: datetime,
    leads: tuple[int, ...],
    already_sent: set[tuple[int, int]],
    tz: tzinfo,
) -> list[Reminder]:
    """The reminders to send right now, and `already_sent` updated in place.

    Only the NEAREST crossed lead produces a message. Without that, a bot restarted five minutes
    before a session - or one whose poll was delayed - would deliver "in 60 minutes", "in 30
    minutes" and "in 10 minutes" in one burst, all of them false. The skipped leads are still
    recorded as sent so they cannot fire later.
    """
    out: list[Reminder] = []
    for session in sessions or []:
        if not isinstance(session, dict):
            continue
        session_id = session.get("id")
        if not isinstance(session_id, int):
            continue
        if session.get("isCompleted") is True:
            continue
        start = parse_local(session.get("startTime"), tz)
        if start is None or start <= now:
            # Already started: whatever was going to be said about it is no longer true.
            continue
        crossed = [lead for lead in leads if now >= start - timedelta(minutes=lead)]
        if not crossed:
            continue
        nearest = min(crossed)
        if (session_id, nearest) not in already_sent:
            out.append(Reminder(session_id, nearest, format_reminder(session, nearest, start)))
        for lead in crossed:
            already_sent.add((session_id, lead))
    return out


def forget_past(
    sessions: list[dict[str, Any]],
    now: datetime,
    already_sent: set[tuple[int, int]],
    tz: tzinfo,
) -> None:
    """Drops bookkeeping for sessions that have started, so the set cannot grow without bound in
    a process that runs for months."""
    live = set()
    for session in sessions or []:
        if not isinstance(session, dict):
            continue
        session_id = session.get("id")
        start = parse_local(session.get("startTime"), tz) if isinstance(session_id, int) else None
        if start is not None and start > now:
            live.add(session_id)
    already_sent.difference_update({key for key in already_sent if key[0] not in live})


class ReminderLoop:
    """Polls every connected account's planned sessions and messages its chat as each lead time
    is crossed.

    Two intervals rather than one: /api/sessions returns the whole session list with no date
    window (SessionService.GetAllAsync), so pulling it for every account on every tick would be
    wasteful. The lists are refreshed on the slower interval and evaluated against the clock on
    the faster one. Consequence worth knowing: a session created less than `refresh_seconds`
    before it starts may miss its earliest lead.

    Bookkeeping is per chat throughout. One account's failure - a revoked key, an instance that
    is down - must not stop the others, so each is polled inside its own try.
    """

    def __init__(
        self,
        accounts: Callable[[], Awaitable[list[Any]]],
        fetch: Callable[[Any], Awaitable[list[dict[str, Any]]]],
        send: Callable[[int, str], Awaitable[None]],
        tz: tzinfo,
        leads: tuple[int, ...],
        tick_seconds: int = 30,
        refresh_seconds: int = 300,
    ) -> None:
        self._accounts = accounts
        self._fetch = fetch
        self._send = send
        self._tz = tz
        self._leads = leads
        self._tick = max(5, tick_seconds)
        self._refresh = max(self._tick, refresh_seconds)
        self._sent: dict[int, set[tuple[int, int]]] = {}
        self._sessions: dict[int, list[dict[str, Any]]] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if not self._leads:
            logger.info("Session reminders are switched off (no lead times configured).")
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        since_refresh = self._refresh  # fetch immediately on the first pass
        while True:
            try:
                accounts = await self._accounts()
                live_chats = {account.chat_id for account in accounts}
                # A chat that disconnected keeps no bookkeeping, and cannot be messaged again.
                for chat_id in set(self._sent) - live_chats:
                    self._sent.pop(chat_id, None)
                    self._sessions.pop(chat_id, None)

                if since_refresh >= self._refresh:
                    for account in accounts:
                        await self._refresh_one(account)
                    since_refresh = 0

                now = datetime.now(self._tz)
                for chat_id, sessions in self._sessions.items():
                    sent = self._sent.setdefault(chat_id, set())
                    forget_past(sessions, now, sent, self._tz)
                    for reminder in due_reminders(sessions, now, self._leads, sent, self._tz):
                        await self._send(chat_id, reminder.text)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One failed pass must not end the loop - the API being briefly unreachable is an
                # ordinary event, and a dead task would silently stop every future reminder.
                logger.exception("A reminder pass failed; continuing")
            await asyncio.sleep(self._tick)
            since_refresh += self._tick

    async def _refresh_one(self, account: Any) -> None:
        try:
            self._sessions[account.chat_id] = await self._fetch(account)
        except Exception:
            # Keeps whatever was last known for this chat rather than dropping it: a single
            # failed refresh should not cancel reminders that are already scheduled.
            logger.warning(
                "Could not refresh sessions for chat %s; keeping the previous list",
                account.chat_id,
                exc_info=True,
            )
