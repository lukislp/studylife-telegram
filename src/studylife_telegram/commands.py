"""Command parsing and reply formatting.

Split deliberately into a pure half (parse_command, the format_* functions) and a thin async half
that talks to StudyLife, so the parts most likely to be wrong - argument splitting, the shapes
returned by the metrics API, hour arithmetic - are testable without a network or a bot token.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import Any

from .studylife_client import StudyLifeApiError, StudyLifeClient
from .times import parse_local, sum_hours_on

# Focus/break lengths of StudyLife's built-in presets. Needed because PhaseEndsAt is an absolute
# instant the CLIENT computes - the server only stores what it is handed (TimerStateService just
# rebases it onto its own clock via ClientNow). Custom modes (id >= 100) live in the user's
# settings, which this bot has no scope to read, so a session already using one keeps its mode
# and falls back to the classic focus length for the phase.
BUILT_IN_MODES: dict[int, tuple[str, int, int]] = {
    1: ("Pomodoro Classic", 25, 5),
    2: ("Flow State", 52, 17),
    3: ("Ultradian Rhythm", 90, 20),
    4: ("Claude Mode", 40, 10),
    5: ("Sprint Bursts", 10, 3),
    6: ("Micro Focus", 5, 1),
    7: ("Quick Burst", 15, 3),
    8: ("Deep Dive", 120, 20),
    9: ("Marathon Session", 180, 30),
}
DEFAULT_FOCUS_MINUTES = 25

# Below this, a stopped run is reported rather than written. Short enough to keep a genuine
# one-minute run (which a 60 s threshold silently discarded in the VS Code integration), long
# enough that an accidental /focus followed straight by /stop does not become a session.
MINIMUM_LOGGABLE_SECONDS = 10


class RunTracker:
    """Which course each chat's current run belongs to, and when it began.

    The timer row has no course column (TimerStateEntity), so this cannot be kept server-side.
    Keyed by chat because one process serves every connected account. In memory is sound only
    because the Deployment runs a single replica; a restart mid-run loses the attribution, and
    /stop then stays silent rather than inventing a course.
    """

    __slots__ = ("_runs",)

    def __init__(self) -> None:
        self._runs: dict[int, tuple[int, datetime]] = {}

    def start(self, chat_id: int, course_id: int | None, at: datetime) -> None:
        if course_id is None:
            self._runs.pop(chat_id, None)
        else:
            self._runs[chat_id] = (course_id, at)

    def finish(self, chat_id: int) -> tuple[int, datetime] | None:
        return self._runs.pop(chat_id, None)

    def forget(self, chat_id: int) -> None:
        """Called when a chat disconnects, so a pending run cannot be logged to an account the
        chat no longer has."""
        self._runs.pop(chat_id, None)


# Shown in Telegram's "/" menu. Keep in sync with handle(); a command listed here but not handled
# is worse than one that is missing, because the menu promises it.
COMMANDS: list[tuple[str, str]] = [
    ("status", "Is a focus session running?"),
    ("focus", "Start a focus session, optionally for a course"),
    ("pause", "Pause the running session"),
    ("stop", "Stop the running session"),
    ("today", "Hours studied today and this week"),
    ("next", "Next course goal and its countdown"),
    ("agenda", "Your next planned study sessions"),
    ("courses", "List your courses"),
    ("note", "Save a quick note"),
    ("login", "Connect your StudyLife account"),
    ("logout", "Disconnect this chat"),
    ("whoami", "Which account this chat is connected to"),
    ("help", "What this bot can do"),
]

# Answerable without a connected account. Everything else needs one, and main.py says so rather
# than letting the command fail somewhere deeper with a confusing error.
ACCOUNT_FREE_COMMANDS = frozenset({"login", "logout", "whoami", "help", "start"})


class ParsedCommand:
    __slots__ = ("name", "argument")

    def __init__(self, name: str, argument: str) -> None:
        self.name = name
        self.argument = argument

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ParsedCommand)
            and other.name == self.name
            and other.argument == self.argument
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ParsedCommand({self.name!r}, {self.argument!r})"


def parse_command(text: str | None) -> ParsedCommand | None:
    """Extracts (name, argument) from a message, or None if it is not a command.

    Handles the "/stop@MyBot" form Telegram produces in groups, and is case-insensitive on the
    command itself while leaving the argument exactly as typed - a note must keep its capitals.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, argument = stripped[1:].partition(" ")
    name, _, _bot = head.partition("@")
    if not name:
        return None
    return ParsedCommand(name.lower(), argument.strip())


def format_hours(hours: object) -> str:
    """Decimal hours from the metrics API as "2 h 15 min". Anything non-numeric becomes a dash
    rather than raising - a missing field should not turn a status reply into an error."""
    if not isinstance(hours, int | float) or isinstance(hours, bool):
        return "-"
    total_minutes = int(round(float(hours) * 60))
    if total_minutes < 0:
        total_minutes = 0
    h, m = divmod(total_minutes, 60)
    return f"{h} h {m} min" if h else f"{m} min"


def format_timer(
    state: dict[str, Any], now: datetime | None = None, tz: tzinfo | None = None
) -> str:
    """The timer state in words.

    There is no "paused" flag on the wire - TimerStateEntity has SessionId, IsRunning, IsBreak,
    CurrentRound, TimerModeId and PhaseEndsAt, and nothing else. An "isPaused" key sent in a PUT
    is silently dropped, and read back from a GET it is always absent. Pause is therefore
    expressed the way StudyLife's own client expresses it: the clock stops (isRunning false,
    phaseEndsAt cleared) while the SESSION is kept, and only a stop clears the session too -
    which is what makes the two distinguishable at all.
    """
    session_id = state.get("sessionId")
    if not state.get("isRunning"):
        if isinstance(session_id, int):
            return "A focus session is paused. /focus resumes it."
        return "No focus session is running."

    parts = ["Break is running." if state.get("isBreak") else "Focus is running."]
    if now is not None and tz is not None:
        ends = parse_local(state.get("phaseEndsAt"), tz)
        if ends is not None and ends > now:
            # Rounded UP: with 30 s to go, "0 min left" reads like the phase is already over.
            # A plain +1 was wrong the other way, turning an exact 26:00 into "27 min left".
            remaining = -(-int((ends - now).total_seconds()) // 60)
            parts.append(f"{remaining} min left.")
    mode_id = state.get("timerModeId")
    mode = BUILT_IN_MODES.get(mode_id) if isinstance(mode_id, int) else None
    if mode is not None:
        parts.append(f"Mode: {mode[0]}.")
    round_number = state.get("currentRound")
    if isinstance(round_number, int) and round_number > 0:
        parts.append(f"Round {round_number}.")
    return " ".join(parts)


def format_today(
    metrics: dict[str, Any], history: list[dict[str, Any]], now: datetime, tz: tzinfo
) -> str:
    """Today, this week and the streak.

    "Today" is summed from the session history rather than read off the metrics response:
    MetricsHoursDto carries week, month, total and totalSessions - there is no daily figure, so
    hours["today"] was always None and always rendered as a dash.
    """
    hours = metrics.get("hours")
    hours = hours if isinstance(hours, dict) else {}
    streak = metrics.get("streak")
    streak = streak if isinstance(streak, dict) else {}
    lines = [
        f"Today: {format_hours(sum_hours_on(history, now, tz))}",
        f"This week: {format_hours(hours.get('week'))}",
    ]
    current = streak.get("current")
    if isinstance(current, int):
        lines.append(f"Streak: {current} day{'' if current == 1 else 's'}")
    return "\n".join(lines)


def format_next_goal(metrics: dict[str, Any]) -> str:
    goals = metrics.get("upcomingCourseGoals")
    if not isinstance(goals, list) or not goals:
        return "No upcoming course goals."
    first = goals[0]
    if not isinstance(first, dict):
        return "No upcoming course goals."
    name = first.get("courseName", "Unknown course")
    days = first.get("daysLeft")
    if isinstance(days, int):
        return f"Next: {name} in {days} day{'' if days == 1 else 's'}."
    return f"Next: {name}."


def format_agenda(sessions: list[dict[str, Any]], now: datetime, tz: tzinfo, limit: int = 5) -> str:
    """The next planned sessions - the same list the reminder loop fires from, so checking what
    is coming and checking what will be announced cannot disagree."""
    upcoming = []
    for session in sessions or []:
        if not isinstance(session, dict) or session.get("isCompleted") is True:
            continue
        start = parse_local(session.get("startTime"), tz)
        if start is None or start <= now:
            continue
        upcoming.append((start, session))
    if not upcoming:
        return "Nothing planned."
    upcoming.sort(key=lambda pair: pair[0])
    lines = []
    for start, session in upcoming[:limit]:
        course = session.get("courseName") or "Study session"
        same_day = start.date() == now.date()
        when = start.strftime("%H:%M") if same_day else start.strftime("%a %d.%m. %H:%M")
        lines.append(f"- {when} {course}")
    return "\n".join(lines)


def format_courses(courses: list[dict[str, Any]]) -> str:
    names = [str(c.get("name", "")) for c in courses if isinstance(c, dict) and c.get("name")]
    if not names:
        return "No courses yet."
    # Telegram rejects a message over 4096 characters, which a long catalogue can reach.
    shown = names[:50]
    text = "\n".join(f"- {n}" for n in shown)
    if len(names) > len(shown):
        text += f"\n... and {len(names) - len(shown)} more"
    return text


def format_help() -> str:
    return "\n".join(f"/{name} - {description}" for name, description in COMMANDS)


def _find_course_id(courses: list[dict[str, Any]], wanted: str) -> int | None:
    """Case-insensitive match, exact first then prefix. Returns None on no match OR on an
    ambiguous prefix - silently picking one of several courses would put study time on the
    wrong one, and that data feeds the grade correlations."""
    target = wanted.strip().lower()
    if not target:
        return None
    exact = [c for c in courses if str(c.get("name", "")).lower() == target]
    if len(exact) == 1:
        cid = exact[0].get("id")
        return cid if isinstance(cid, int) else None
    prefix = [c for c in courses if str(c.get("name", "")).lower().startswith(target)]
    if len(prefix) == 1:
        cid = prefix[0].get("id")
        return cid if isinstance(cid, int) else None
    return None


def _wire_time(value: datetime) -> str:
    """One timestamp in the form StudyLife uses everywhere: local wall clock, no offset."""
    return value.replace(tzinfo=None).isoformat(timespec="seconds")


def next_timer_state(
    current: dict[str, Any], action: str, now: datetime, mode_id: int | None = None
) -> dict[str, Any]:
    """The state to PUT for start/pause/stop, built from the fields the server actually has.

    Only these keys are sent. Anything else (isPaused, courseId) is dropped server-side without
    an error, so an invented field costs a feature rather than a failed request - which is how
    "pause" used to behave exactly like "stop" here.
    """
    current_round = current.get("currentRound")
    state: dict[str, Any] = {
        "sessionId": current.get("sessionId"),
        "isRunning": False,
        "isBreak": bool(current.get("isBreak")),
        "currentRound": current_round if isinstance(current_round, int) else 1,
        "timerModeId": mode_id or current.get("timerModeId") or 1,
        "phaseEndsAt": None,
        # Naive local, like every other timestamp StudyLife exchanges: the server compares
        # these against DateTime.Now, and an offset-carrying value relies on .NET converting
        # it back to exactly the same zone. See times.py.
        "clientNow": _wire_time(now),
    }

    if action == "stop":
        # A stop ends the session; keeping the id would leave the next /status calling it paused.
        state["sessionId"] = None
        state["isBreak"] = False
        state["currentRound"] = 1
        return state

    if action == "pause":
        # The clock stops, the session stays. The remaining time cannot be carried - the wire
        # shape has nowhere to put it (StudyLife's own client keeps it in a private field it
        # never sends), so resuming starts the phase over. Documented in README.
        return state

    active_mode = state["timerModeId"]
    preset = BUILT_IN_MODES.get(active_mode) if isinstance(active_mode, int) else None
    minutes = preset[1] if preset is not None else DEFAULT_FOCUS_MINUTES
    state["isRunning"] = True
    state["phaseEndsAt"] = _wire_time(now + timedelta(minutes=minutes))
    return state


async def handle(
    command: ParsedCommand,
    client: StudyLifeClient,
    runs: RunTracker,
    chat_id: int,
    now: datetime,
    tz: tzinfo,
) -> str:
    """Runs one command and returns the reply text. Never raises: a failure the user can act on
    is more useful in the chat than in the pod log.

    `now` is passed in rather than read here so every reply in one delivery is computed against
    a single instant, and so the tests need no clock.
    """
    try:
        return await _dispatch(command, client, runs, chat_id, now, tz)
    except StudyLifeApiError as exc:
        if exc.status_code == 403:
            return (
                "StudyLife refused that (403). This bot was not granted the permission it needs "
                "- re-approve it with the missing scope."
            )
        return f"StudyLife could not be reached right now ({exc.status_code})."


async def _dispatch(
    command: ParsedCommand,
    client: StudyLifeClient,
    runs: RunTracker,
    chat_id: int,
    now: datetime,
    tz: tzinfo,
) -> str:
    name = command.name

    if name in ("help", "start"):
        return format_help()

    if name == "status":
        return format_timer(await client.get_timer_state(), now, tz)

    if name in ("focus", "pause", "stop"):
        return await _timer_transition(command, client, runs, chat_id, now, tz)

    if name == "today":
        return format_today(
            await client.get_metrics_summary(),
            await client.get_session_history(days=2, only_completed=True),
            now,
            tz,
        )

    if name == "next":
        return format_next_goal(await client.get_metrics_summary())

    if name == "agenda":
        return format_agenda(await client.list_sessions(), now, tz)

    if name == "courses":
        return format_courses(await client.list_courses())

    if name == "note":
        if not command.argument:
            return "Usage: /note <text>"
        title = command.argument.splitlines()[0][:80]
        await client.create_note(title, command.argument)
        return "Saved."

    return f"Unknown command. Try /help.\n\n{format_help()}"


async def _timer_transition(
    command: ParsedCommand,
    client: StudyLifeClient,
    runs: RunTracker,
    chat_id: int,
    now: datetime,
    tz: tzinfo,
) -> str:
    current = dict(await client.get_timer_state())

    course_id: int | None = None
    if command.name == "focus" and command.argument:
        courses = await client.list_courses()
        course_id = _find_course_id(courses, command.argument)
        if course_id is None:
            return (
                f'No single course matches "{command.argument}". '
                "Use /courses to see the exact names."
            )

    # Deliberately no clientSequence: the server accepts a missing one as plain last-write-wins
    # (see TimerStateService), and a per-process counter would be meaningless here anyway - each
    # webhook delivery may land in a different replica.
    saved = await client.save_timer_state(next_timer_state(current, command.name, now))
    reply = format_timer(saved, now, tz)

    if command.name == "focus":
        # The timer row has no course column, so the course cannot ride along with the state.
        # It is remembered here and turned into a logged session on /stop, the same way the
        # VS Code integration does it.
        runs.start(chat_id, course_id, now)
        if course_id is not None:
            reply = f"{reply} It will be logged to that course when you /stop."
    elif command.name == "stop":
        note = await _log_run(runs, client, chat_id, now)
        if note:
            reply = f"{reply} {note}"

    return reply


async def _log_run(runs: RunTracker, client: StudyLifeClient, chat_id: int, now: datetime) -> str:
    """Turns a finished run into a session, or says why it did not become one.

    Silence would be the wrong answer here: a run that quietly fails to be logged looks
    identical to one that was, and the missing hours only surface days later in the statistics.
    """
    run = runs.finish(chat_id)
    if run is None:
        return ""
    course_id, started_at = run
    seconds = (now - started_at).total_seconds()
    if seconds < MINIMUM_LOGGABLE_SECONDS:
        return f"Too short to log ({int(seconds)} s)."
    await client.create_session(course_id, started_at, now, "Telegram focus session")
    return f"Logged {int(round(seconds / 60))} min."
