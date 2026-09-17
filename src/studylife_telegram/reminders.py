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
from datetime import datetime, timedelta, tzinfo
from typing import Any, NamedTuple, Protocol

from .times import parse_local

logger = logging.getLogger(__name__)


class Reminder(NamedTuple):
    session_id: int
    minutes_before: int
    text: str


class Sender(Protocol):
    async def __call__(self, text: str) -> None: ...


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
    """Polls planned sessions and sends a message as each lead time is crossed.

    Two intervals rather than one: /api/sessions returns the whole session list with no date
    window (SessionService.GetAllAsync), so pulling it every tick just to notice that nothing
    changed would be wasteful. The list is refreshed on the slower interval and evaluated
    against the clock on the faster one. Consequence worth knowing: a session created less than
    `refresh_seconds` before it starts may miss its earliest lead.
    """

    def __init__(
        self,
        fetch: Any,
        send: Sender,
        tz: tzinfo,
        leads: tuple[int, ...],
        tick_seconds: int = 30,
        refresh_seconds: int = 300,
    ) -> None:
        self._fetch = fetch
        self._send = send
        self._tz = tz
        self._leads = leads
        self._tick = max(5, tick_seconds)
        self._refresh = max(self._tick, refresh_seconds)
        self._sent: set[tuple[int, int]] = set()
        self._sessions: list[dict[str, Any]] = []
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
                if since_refresh >= self._refresh:
                    self._sessions = await self._fetch()
                    since_refresh = 0
                now = datetime.now(self._tz)
                forget_past(self._sessions, now, self._sent, self._tz)
                for reminder in due_reminders(
                    self._sessions, now, self._leads, self._sent, self._tz
                ):
                    await self._send(reminder.text)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One failed poll must not end the loop - the API being briefly unreachable is
                # an ordinary event, and a dead task would silently stop every future reminder.
                logger.exception("A reminder poll failed; continuing")
            await asyncio.sleep(self._tick)
            since_refresh += self._tick
