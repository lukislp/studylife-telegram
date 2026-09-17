from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from studylife_telegram.commands import (
    ParsedCommand,
    RunTracker,
    _find_course_id,
    format_agenda,
    format_courses,
    format_hours,
    format_next_goal,
    format_timer,
    format_today,
    handle,
    next_timer_state,
    parse_command,
)
from studylife_telegram.studylife_client import StudyLifeApiError

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=TZ)


def at(hour: int, minute: int = 0) -> str:
    """A StudyLife timestamp: local wall clock, no offset - exactly what the server sends."""
    return NOW.replace(hour=hour, minute=minute).replace(tzinfo=None).isoformat()


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
        assert format_timer({}) == "No focus session is running."
        assert "running" in format_timer({"isRunning": True})

    def test_a_kept_session_with_a_stopped_clock_is_a_pause(self) -> None:
        # The only way the two are distinguishable: stop clears sessionId, pause keeps it.
        # There is no isPaused field on TimerStateEntity for either of them to use.
        assert "paused" in format_timer({"isRunning": False, "sessionId": 42})
        assert "paused" not in format_timer({"isRunning": False, "sessionId": None})

    def test_running_timer_reports_phase_mode_round_and_remaining(self) -> None:
        text = format_timer(
            {
                "isRunning": True,
                "isBreak": False,
                "timerModeId": 2,
                "currentRound": 3,
                "phaseEndsAt": at(12, 26),
            },
            NOW,
            TZ,
        )
        assert "Focus is running." in text
        assert "26 min left." in text
        assert "Flow State" in text
        assert "Round 3" in text

    def test_break_is_named_as_a_break(self) -> None:
        assert "Break is running." in format_timer({"isRunning": True, "isBreak": True}, NOW, TZ)

    def test_today_survives_missing_sections(self) -> None:
        assert "Today: -" not in format_today({}, [], NOW, TZ)
        assert "Today: 0 min" in format_today({}, [], NOW, TZ)
        text = format_today(
            {"hours": {"week": 7.0}, "streak": {"current": 1}},
            [{"startTime": at(9, 0), "endTime": at(10, 30)}],
            NOW,
            TZ,
        )
        assert "Today: 1 h 30 min" in text
        assert "This week: 7 h 0 min" in text
        assert "Streak: 1 day" in text

    def test_today_pluralises_the_streak(self) -> None:
        assert "Streak: 3 days" in format_today({"streak": {"current": 3}}, [], NOW, TZ)

    def test_today_is_summed_from_sessions_not_read_off_the_metrics_response(self) -> None:
        # MetricsHoursDto has no daily figure at all, so a "today" key there must be ignored -
        # believing it is what made this render as a dash.
        text = format_today(
            {"hours": {"today": 99.0, "week": 7.0}},
            [{"startTime": at(8, 0), "endTime": at(9, 15)}],
            NOW,
            TZ,
        )
        assert "Today: 1 h 15 min" in text

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
        self.history: list[dict[str, Any]] = overrides.get("history", [])
        self.sessions: list[dict[str, Any]] = overrides.get("sessions", [])
        self.raises: Exception | None = overrides.get("raises")
        self.saved: dict[str, Any] | None = None
        self.notes: list[tuple[str, str]] = []
        self.created: list[dict[str, Any]] = []

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

    async def get_session_history(
        self, days: int = 2, only_completed: bool = True
    ) -> list[dict[str, Any]]:
        if self.raises:
            raise self.raises
        return list(self.history)

    async def list_sessions(self) -> list[dict[str, Any]]:
        if self.raises:
            raise self.raises
        return list(self.sessions)

    async def create_session(
        self, course_id: int, start_time: Any, end_time: Any, topic: str | None = None
    ) -> dict[str, Any]:
        self.created.append(
            {"courseId": course_id, "startTime": start_time, "endTime": end_time, "topic": topic}
        )
        return {"id": 99}

    async def create_note(self, title: str, content: str) -> dict[str, Any]:
        self.notes.append((title, content))
        return {"id": 1}


async def run(
    command: ParsedCommand,
    client: FakeClient,
    runs: RunTracker | None = None,
    now: datetime = NOW,
) -> str:
    return await handle(command, client, runs or RunTracker(), now, TZ)


class TestNextTimerState:
    """The payload, which is where the silent failures live: the server drops unknown keys
    without complaining, so a wrong field name costs a feature and never an error."""

    def test_sends_only_fields_the_server_actually_has(self) -> None:
        state = next_timer_state({"isRunning": False}, "focus", NOW)
        assert set(state) == {
            "sessionId",
            "isRunning",
            "isBreak",
            "currentRound",
            "timerModeId",
            "phaseEndsAt",
            "clientNow",
        }

    def test_pause_stops_the_clock_but_keeps_the_session(self) -> None:
        state = next_timer_state(
            {"isRunning": True, "sessionId": 42, "isBreak": True, "currentRound": 3}, "pause", NOW
        )
        assert state["isRunning"] is False
        assert state["phaseEndsAt"] is None
        assert state["sessionId"] == 42
        assert state["currentRound"] == 3
        assert state["isBreak"] is True

    def test_stop_ends_the_session_and_resets_the_round(self) -> None:
        state = next_timer_state(
            {"isRunning": True, "sessionId": 42, "isBreak": True, "currentRound": 3}, "stop", NOW
        )
        assert state["sessionId"] is None
        assert state["currentRound"] == 1
        assert state["isBreak"] is False
        assert state["phaseEndsAt"] is None

    def test_start_sets_the_phase_end_from_the_mode(self) -> None:
        state = next_timer_state({"isRunning": False, "timerModeId": 3}, "focus", NOW)
        assert state["isRunning"] is True
        # Ultradian Rhythm: 90 minutes of focus.
        assert state["phaseEndsAt"] == "2026-09-17T13:30:00"

    def test_an_unknown_custom_mode_falls_back_to_a_classic_phase(self) -> None:
        # Custom modes (id >= 100) live in settings this bot cannot read; the mode is kept.
        state = next_timer_state({"isRunning": False, "timerModeId": 137}, "focus", NOW)
        assert state["timerModeId"] == 137
        assert state["phaseEndsAt"] == "2026-09-17T12:25:00"


class TestAgenda:
    def test_lists_the_next_planned_sessions_in_order(self) -> None:
        sessions = [
            {"id": 2, "courseName": "Physik", "startTime": at(16, 0)},
            {"id": 1, "courseName": "Mathe", "startTime": at(14, 0)},
            {"id": 3, "courseName": "Already past", "startTime": at(9, 0)},
            {"id": 4, "courseName": "Done", "startTime": at(18, 0), "isCompleted": True},
        ]
        assert format_agenda(sessions, NOW, TZ).splitlines() == [
            "- 14:00 Mathe",
            "- 16:00 Physik",
        ]

    def test_says_so_when_nothing_is_planned(self) -> None:
        assert format_agenda([], NOW, TZ) == "Nothing planned."


@pytest.mark.asyncio
class TestHandle:
    async def test_status(self) -> None:
        client = FakeClient(timer={"isRunning": True})
        assert "running" in await run(ParsedCommand("status", ""), client)

    async def test_focus_starts_the_clock(self) -> None:
        client = FakeClient(timer={"isRunning": False, "sessionId": 42})
        await run(ParsedCommand("focus", ""), client)
        assert client.saved is not None
        assert client.saved["isRunning"] is True
        assert client.saved["phaseEndsAt"] is not None

    async def test_pause_is_not_a_stop(self) -> None:
        # The bug this replaces: pause sent isPaused (which the server drops) together with
        # isRunning false, which is simply a stop - the session was lost every time.
        client = FakeClient(timer={"isRunning": True, "sessionId": 42, "currentRound": 2})
        reply = await run(ParsedCommand("pause", ""), client)
        assert client.saved is not None
        assert client.saved["sessionId"] == 42
        assert client.saved["isRunning"] is False
        assert "paused" in reply

    async def test_stop_clears_the_session(self) -> None:
        client = FakeClient(timer={"isRunning": True, "sessionId": 42})
        await run(ParsedCommand("stop", ""), client)
        assert client.saved is not None
        assert client.saved["sessionId"] is None

    async def test_focus_with_an_unknown_course_writes_nothing(self) -> None:
        client = FakeClient(courses=[{"id": 7, "name": "Mathe"}])
        reply = await run(ParsedCommand("focus", "Physik"), client)
        assert "No single course matches" in reply
        assert client.saved is None

    async def test_a_run_with_a_course_becomes_a_session_on_stop(self) -> None:
        # The timer row has no course column, so this is the only way the picked course reaches
        # the history at all.
        client = FakeClient(courses=[{"id": 7, "name": "Mathe"}])
        runs = RunTracker()
        await run(ParsedCommand("focus", "Mathe"), client, runs)
        reply = await run(ParsedCommand("stop", ""), client, runs, NOW + timedelta(minutes=25))
        assert len(client.created) == 1
        assert client.created[0]["courseId"] == 7
        assert "Logged 25 min" in reply

    async def test_a_run_without_a_course_logs_nothing(self) -> None:
        client = FakeClient()
        runs = RunTracker()
        await run(ParsedCommand("focus", ""), client, runs)
        reply = await run(ParsedCommand("stop", ""), client, runs, NOW + timedelta(minutes=25))
        assert client.created == []
        assert "Logged" not in reply

    async def test_a_run_too_short_to_log_says_so_instead_of_going_quiet(self) -> None:
        client = FakeClient(courses=[{"id": 7, "name": "Mathe"}])
        runs = RunTracker()
        await run(ParsedCommand("focus", "Mathe"), client, runs)
        reply = await run(ParsedCommand("stop", ""), client, runs, NOW + timedelta(seconds=4))
        assert client.created == []
        assert "Too short" in reply

    async def test_a_stale_client_sequence_is_never_sent_back(self) -> None:
        # Echoing the server's counter would make our own write look older than it is.
        client = FakeClient(timer={"isRunning": False, "clientSequence": 41})
        await run(ParsedCommand("focus", ""), client)
        assert client.saved is not None
        assert "clientSequence" not in client.saved

    async def test_today_uses_the_session_history(self) -> None:
        client = FakeClient(
            metrics={"hours": {"week": 7.0}},
            history=[{"startTime": at(10, 0), "endTime": at(11, 30)}],
        )
        assert "Today: 1 h 30 min" in await run(ParsedCommand("today", ""), client)

    async def test_note_requires_an_argument(self) -> None:
        client = FakeClient()
        assert await run(ParsedCommand("note", ""), client) == "Usage: /note <text>"
        assert client.notes == []

    async def test_note_uses_the_first_line_as_the_title(self) -> None:
        client = FakeClient()
        await run(ParsedCommand("note", "Kapitel 3\nAlles zu Seitenersetzung"), client)
        assert client.notes[0][0] == "Kapitel 3"

    async def test_unknown_command_offers_help(self) -> None:
        assert "Unknown command" in await run(ParsedCommand("dance", ""), FakeClient())

    async def test_a_permission_error_says_what_to_do(self) -> None:
        client = FakeClient(raises=StudyLifeApiError(403, "forbidden"))
        assert "not granted" in await run(ParsedCommand("status", ""), client)

    async def test_other_api_errors_do_not_escape(self) -> None:
        client = FakeClient(raises=StudyLifeApiError(502, "bad gateway"))
        assert "502" in await run(ParsedCommand("today", ""), client)
