"""Command parsing and reply formatting.

Split deliberately into a pure half (parse_command, the format_* functions) and a thin async half
that talks to StudyLife, so the parts most likely to be wrong - argument splitting, the shapes
returned by the metrics API, hour arithmetic - are testable without a network or a bot token.
"""

from __future__ import annotations

from typing import Any

from .studylife_client import StudyLifeApiError, StudyLifeClient

# Shown in Telegram's "/" menu. Keep in sync with handle(); a command listed here but not handled
# is worse than one that is missing, because the menu promises it.
COMMANDS: list[tuple[str, str]] = [
    ("status", "Is a focus session running?"),
    ("focus", "Start a focus session, optionally for a course"),
    ("pause", "Pause the running session"),
    ("stop", "Stop the running session"),
    ("today", "Hours studied today and this week"),
    ("next", "Next course goal and its countdown"),
    ("courses", "List your courses"),
    ("note", "Save a quick note"),
    ("help", "What this bot can do"),
]


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


def format_timer(state: dict[str, Any]) -> str:
    if not state.get("isRunning"):
        return "No focus session is running."
    if state.get("isPaused"):
        return "A focus session is paused."
    return "A focus session is running."


def format_today(metrics: dict[str, Any]) -> str:
    hours = metrics.get("hours")
    hours = hours if isinstance(hours, dict) else {}
    streak = metrics.get("streak")
    streak = streak if isinstance(streak, dict) else {}
    lines = [
        f"Today: {format_hours(hours.get('today'))}",
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


async def handle(command: ParsedCommand, client: StudyLifeClient) -> str:
    """Runs one command and returns the reply text. Never raises: a failure the user can act on
    is more useful in the chat than in the pod log."""
    try:
        return await _dispatch(command, client)
    except StudyLifeApiError as exc:
        if exc.status_code == 403:
            return (
                "StudyLife refused that (403). This bot was not granted the permission it needs "
                "- re-approve it with the missing scope."
            )
        return f"StudyLife could not be reached right now ({exc.status_code})."


async def _dispatch(command: ParsedCommand, client: StudyLifeClient) -> str:
    name = command.name

    if name in ("help", "start"):
        return format_help()

    if name == "status":
        return format_timer(await client.get_timer_state())

    if name in ("focus", "pause", "stop"):
        return await _timer_transition(command, client)

    if name == "today":
        return format_today(await client.get_metrics_summary())

    if name == "next":
        return format_next_goal(await client.get_metrics_summary())

    if name == "courses":
        return format_courses(await client.list_courses())

    if name == "note":
        if not command.argument:
            return "Usage: /note <text>"
        title = command.argument.splitlines()[0][:80]
        await client.create_note(title, command.argument)
        return "Saved."

    return f"Unknown command. Try /help.\n\n{format_help()}"


async def _timer_transition(command: ParsedCommand, client: StudyLifeClient) -> str:
    state = dict(await client.get_timer_state())

    if command.name == "focus" and command.argument:
        courses = await client.list_courses()
        course_id = _find_course_id(courses, command.argument)
        if course_id is None:
            return (
                f'No single course matches "{command.argument}". '
                "Use /courses to see the exact names."
            )
        state["courseId"] = course_id

    state["isRunning"] = command.name != "stop"
    state["isPaused"] = command.name == "pause"
    # Deliberately no clientSequence: the server accepts a missing one as plain last-write-wins
    # (see TimerStateService), and a per-process counter would be meaningless here anyway - each
    # webhook delivery may land in a different replica.
    state.pop("clientSequence", None)

    return format_timer(await client.save_timer_state(state))
