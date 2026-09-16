from typing import Any

import pytest

from studylife_telegram.commands import (
    ParsedCommand,
    _find_course_id,
    format_courses,
    format_hours,
    format_next_goal,
    format_timer,
    format_today,
    handle,
    parse_command,
)
from studylife_telegram.studylife_client import StudyLifeApiError


class TestParseCommand:
    def test_parses_a_bare_command(self) -> None:
        assert parse_command("/status") == ParsedCommand("status", "")

    def test_parses_a_command_with_an_argument(self) -> None:
        assert parse_command("/focus Betriebssysteme") == ParsedCommand("focus", "Betriebssysteme")

    def test_strips_the_bot_suffix_telegram_adds_in_groups(self) -> None:
        assert parse_command("/stop@StudyLifeBot") == ParsedCommand("stop", "")
        assert parse_command("/focus@StudyLifeBot Mathe") == ParsedCommand("focus", "Mathe")

    def test_lowercases_the_command_but_not_the_argument(self) -> None:
        assert parse_command("/NOTE Kapitel 3 gelesen") == ParsedCommand(
            "note", "Kapitel 3 gelesen"
        )

    def test_keeps_a_multi_word_argument_intact(self) -> None:
        assert parse_command("/note a b  c").argument == "a b  c"

    def test_returns_none_for_plain_text_and_edge_cases(self) -> None:
        assert parse_command("hello") is None
        assert parse_command("") is None
        assert parse_command(None) is None
        assert parse_command("/") is None
        assert parse_command("   ") is None

    def test_tolerates_surrounding_whitespace(self) -> None:
        assert parse_command("  /status  ") == ParsedCommand("status", "")


class TestFormatHours:
    def test_renders_hours_and_minutes(self) -> None:
        assert format_hours(2.25) == "2 h 15 min"
        assert format_hours(0.75) == "45 min"
        assert format_hours(0) == "0 min"
        assert format_hours(2) == "2 h 0 min"

    def test_returns_a_dash_for_anything_not_numeric(self) -> None:
        assert format_hours(None) == "-"
        assert format_hours("2.5") == "-"
        # bool is an int in Python; treating True as "1 hour" would be nonsense.
        assert format_hours(True) == "-"

    def test_clamps_a_negative_value(self) -> None:
        assert format_hours(-1.0) == "0 min"


class TestFormatters:
    def test_timer_states(self) -> None:
        assert format_timer({"isRunning": False}) == "No focus session is running."
        assert format_timer({"isRunning": True, "isPaused": True}) == "A focus session is paused."
        assert format_timer({"isRunning": True}) == "A focus session is running."
        assert format_timer({}) == "No focus session is running."

    def test_today_survives_missing_sections(self) -> None:
        assert "Today: -" in format_today({})
        text = format_today({"hours": {"today": 1.5, "week": 7.0}, "streak": {"current": 1}})
        assert "Today: 1 h 30 min" in text
        assert "This week: 7 h 0 min" in text
        assert "Streak: 1 day" in text

    def test_today_pluralises_the_streak(self) -> None:
        assert "Streak: 3 days" in format_today({"streak": {"current": 3}})

    def test_next_goal(self) -> None:
        assert format_next_goal({}) == "No upcoming course goals."
        assert format_next_goal({"upcomingCourseGoals": []}) == "No upcoming course goals."
        text = format_next_goal({"upcomingCourseGoals": [{"courseName": "Mathe", "daysLeft": 1}]})
        assert text == "Next: Mathe in 1 day."

    def test_courses_truncates_a_long_catalogue(self) -> None:
        # Telegram rejects messages over 4096 characters.
        many = [{"name": f"Course {i}"} for i in range(80)]
        text = format_courses(many)
        assert "and 30 more" in text
        assert len(text) < 4096

    def test_courses_when_there_are_none(self) -> None:
        assert format_courses([]) == "No courses yet."
        assert format_courses([{"id": 1}]) == "No courses yet."


class TestFindCourseId:
    courses: list[dict[str, Any]] = [
        {"id": 1, "name": "Betriebssysteme"},
        {"id": 2, "name": "Betriebswirtschaft"},
        {"id": 3, "name": "Mathe"},
    ]

    def test_matches_exactly_regardless_of_case(self) -> None:
        assert _find_course_id(self.courses, "mathe") == 3
        assert _find_course_id(self.courses, "  Mathe ") == 3

    def test_matches_an_unambiguous_prefix(self) -> None:
        assert _find_course_id(self.courses, "Betriebssys") == 1

    def test_refuses_an_ambiguous_prefix_rather_than_guessing(self) -> None:
        # Putting study time on the wrong course would distort the grade correlations, so an
        # ambiguous match must fail loudly instead of picking one.
        assert _find_course_id(self.courses, "Betrieb") is None

    def test_returns_none_for_no_match_or_empty_input(self) -> None:
        assert _find_course_id(self.courses, "Physik") is None
        assert _find_course_id(self.courses, "") is None

    def test_prefers_an_exact_match_over_a_prefix(self) -> None:
        courses = [{"id": 1, "name": "Mathe"}, {"id": 2, "name": "Mathe II"}]
        assert _find_course_id(courses, "Mathe") == 1


class FakeClient:
    """Stands in for StudyLifeClient. Records what was written so the timer tests can assert on
    the payload rather than only on the reply text."""

    def __init__(self, **overrides: Any) -> None:
        self.timer: dict[str, Any] = overrides.get("timer", {"isRunning": False})
        self.courses: list[dict[str, Any]] = overrides.get("courses", [])
        self.metrics: dict[str, Any] = overrides.get("metrics", {})
        self.raises: Exception | None = overrides.get("raises")
        self.saved: dict[str, Any] | None = None
        self.notes: list[tuple[str, str]] = []

    async def get_timer_state(self) -> dict[str, Any]:
        if self.raises:
            raise self.raises
        return dict(self.timer)

    async def save_timer_state(self, state: dict[str, Any]) -> dict[str, Any]:
        self.saved = dict(state)
        return dict(state)

    async def list_courses(self) -> list[dict[str, Any]]:
        if self.raises:
            raise self.raises
        return list(self.courses)

    async def get_metrics_summary(self) -> dict[str, Any]:
        if self.raises:
            raise self.raises
        return dict(self.metrics)

    async def create_note(self, title: str, content: str) -> dict[str, Any]:
        self.notes.append((title, content))
        return {"id": 1}


@pytest.mark.asyncio
class TestHandle:
    async def test_status(self) -> None:
        client = FakeClient(timer={"isRunning": True})
        assert await handle(ParsedCommand("status", ""), client) == "A focus session is running."

    async def test_focus_starts_and_clears_paused(self) -> None:
        client = FakeClient(timer={"isRunning": False, "isPaused": True})
        await handle(ParsedCommand("focus", ""), client)
        assert client.saved == {"isRunning": True, "isPaused": False}

    async def test_focus_with_a_course_sets_the_id(self) -> None:
        client = FakeClient(courses=[{"id": 7, "name": "Mathe"}])
        await handle(ParsedCommand("focus", "Mathe"), client)
        assert client.saved is not None
        assert client.saved["courseId"] == 7

    async def test_focus_with_an_unknown_course_writes_nothing(self) -> None:
        client = FakeClient(courses=[{"id": 7, "name": "Mathe"}])
        reply = await handle(ParsedCommand("focus", "Physik"), client)
        assert "No single course matches" in reply
        assert client.saved is None

    async def test_stop_and_pause(self) -> None:
        client = FakeClient(timer={"isRunning": True})
        await handle(ParsedCommand("stop", ""), client)
        assert client.saved == {"isRunning": False, "isPaused": False}

        client = FakeClient(timer={"isRunning": True})
        await handle(ParsedCommand("pause", ""), client)
        assert client.saved == {"isRunning": True, "isPaused": True}

    async def test_a_stale_client_sequence_is_never_sent_back(self) -> None:
        # Echoing the server's counter would make our own write look older than it is.
        client = FakeClient(timer={"isRunning": False, "clientSequence": 41})
        await handle(ParsedCommand("focus", ""), client)
        assert client.saved is not None
        assert "clientSequence" not in client.saved

    async def test_note_requires_an_argument(self) -> None:
        client = FakeClient()
        assert await handle(ParsedCommand("note", ""), client) == "Usage: /note <text>"
        assert client.notes == []

    async def test_note_uses_the_first_line_as_the_title(self) -> None:
        client = FakeClient()
        await handle(ParsedCommand("note", "Kapitel 3\nAlles über Seitenersetzung"), client)
        assert client.notes[0][0] == "Kapitel 3"

    async def test_unknown_command_offers_help(self) -> None:
        assert "Unknown command" in await handle(ParsedCommand("dance", ""), FakeClient())

    async def test_a_permission_error_says_what_to_do(self) -> None:
        client = FakeClient(raises=StudyLifeApiError(403, "forbidden"))
        reply = await handle(ParsedCommand("status", ""), client)
        assert "not granted" in reply

    async def test_other_api_errors_do_not_escape(self) -> None:
        client = FakeClient(raises=StudyLifeApiError(502, "bad gateway"))
        reply = await handle(ParsedCommand("today", ""), client)
        assert "502" in reply
