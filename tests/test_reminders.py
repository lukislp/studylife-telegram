from datetime import datetime
from zoneinfo import ZoneInfo

from studylife_telegram.config import Settings
from studylife_telegram.reminders import due_reminders, forget_past, format_reminder
from studylife_telegram.times import parse_local, sum_hours_on, zone

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=TZ)
LEADS = (60, 30, 10, 5, 3, 2, 1)


def at(hour: int, minute: int = 0) -> str:
    """A StudyLife timestamp: local wall clock, no offset - exactly what the server sends."""
    return NOW.replace(hour=hour, minute=minute).replace(tzinfo=None).isoformat()


def session(session_id: int, start: str, **extra: object) -> dict[str, object]:
    return {"id": session_id, "courseName": "Mathe", "startTime": start, **extra}


class TestParseLocal:
    def test_reads_a_naive_timestamp_in_the_configured_zone(self) -> None:
        # The whole point: the pod runs UTC, the server means Berlin. Parsing this with the
        # process clock would put every reminder two hours out.
        parsed = parse_local("2026-09-17T14:00:00", TZ)
        assert parsed == datetime(2026, 9, 17, 14, 0, tzinfo=TZ)

    def test_honours_an_offset_when_one_is_present(self) -> None:
        # The server does not send one today; a future version that does must not be re-shifted.
        parsed = parse_local("2026-09-17T14:00:00+00:00", TZ)
        assert parsed is not None
        assert parsed.astimezone(TZ).hour == 16

    def test_returns_none_for_anything_unusable(self) -> None:
        for value in (None, "", "not a date", 17, "2026-13-45T99:00:00"):
            assert parse_local(value, TZ) is None


class TestZone:
    def test_falls_back_to_utc_rather_than_crashing_on_a_bad_name(self) -> None:
        # A typo in the env var should cost correct reminder times, not the whole bot.
        assert zone("Mars/Olympus_Mons") is not None
        assert str(zone("Europe/Berlin")) == "Europe/Berlin"


class TestSumHoursOn:
    def test_sums_todays_sessions(self) -> None:
        history = [
            {"startTime": at(9, 0), "endTime": at(10, 30)},
            {"startTime": at(13, 0), "endTime": at(14, 0)},
        ]
        assert sum_hours_on(history, NOW, TZ) == 2.5

    def test_clips_a_session_that_spans_midnight(self) -> None:
        history = [{"startTime": "2026-09-16T23:00:00", "endTime": "2026-09-17T01:00:00"}]
        assert sum_hours_on(history, NOW, TZ) == 1.0

    def test_ignores_other_days_and_malformed_rows(self) -> None:
        history = [
            {"startTime": "2026-09-15T09:00:00", "endTime": "2026-09-15T10:00:00"},
            {"startTime": at(9, 0)},
            {"startTime": at(10, 0), "endTime": at(9, 0)},
            "not a dict",
        ]
        assert sum_hours_on(history, NOW, TZ) == 0.0

    def test_an_empty_history_is_zero_not_a_dash(self) -> None:
        assert sum_hours_on([], NOW, TZ) == 0.0


class TestDueReminders:
    def test_fires_as_each_lead_is_crossed(self) -> None:
        sessions = [session(1, at(13, 0))]
        sent: set[tuple[int, int]] = set()

        # Before the first lead time, nothing at all.
        assert due_reminders(sessions, NOW.replace(hour=11, minute=59), LEADS, sent, TZ) == []

        for minute, expected in ((0, 60), (30, 30), (50, 10), (55, 5), (59, 1)):
            due = due_reminders(sessions, NOW.replace(hour=12, minute=minute), LEADS, sent, TZ)
            assert [r.minutes_before for r in due] == [expected]

    def test_never_repeats_the_same_lead(self) -> None:
        sessions = [session(1, at(13, 0))]
        sent: set[tuple[int, int]] = set()
        first = due_reminders(sessions, NOW.replace(hour=12, minute=30), LEADS, sent, TZ)
        second = due_reminders(sessions, NOW.replace(hour=12, minute=31), LEADS, sent, TZ)
        assert len(first) == 1
        assert second == []

    def test_a_late_start_sends_only_the_nearest_lead_not_all_of_them(self) -> None:
        # Restarting five minutes before a session must not deliver "in 60 minutes",
        # "in 30 minutes" and "in 10 minutes" in one burst - all of them would be false.
        sessions = [session(1, at(13, 0))]
        sent: set[tuple[int, int]] = set()
        due = due_reminders(sessions, NOW.replace(hour=12, minute=57), LEADS, sent, TZ)
        assert [r.minutes_before for r in due] == [3]
        # And the leads it skipped can never fire afterwards.
        later = due_reminders(sessions, NOW.replace(hour=12, minute=58), LEADS, sent, TZ)
        assert [r.minutes_before for r in later] == [2]

    def test_ignores_sessions_that_already_started_or_are_completed(self) -> None:
        sessions = [
            session(1, at(11, 0)),
            session(2, at(13, 0), isCompleted=True),
        ]
        sent: set[tuple[int, int]] = set()
        assert due_reminders(sessions, NOW.replace(hour=12, minute=30), LEADS, sent, TZ) == []

    def test_survives_malformed_entries(self) -> None:
        sessions = [
            "not a dict",
            {"id": "seven", "startTime": at(13, 0)},
            {"id": 3, "startTime": "nonsense"},
            {"id": 4},
        ]
        sent: set[tuple[int, int]] = set()
        assert due_reminders(sessions, NOW.replace(hour=12, minute=30), LEADS, sent, TZ) == []

    def test_no_leads_configured_means_no_reminders(self) -> None:
        sent: set[tuple[int, int]] = set()
        assert due_reminders([session(1, at(13, 0))], NOW, (), sent, TZ) == []


class TestForgetPast:
    def test_drops_bookkeeping_for_sessions_that_have_started(self) -> None:
        # Otherwise the set grows for as long as the process lives.
        sent = {(1, 30), (1, 10), (2, 60)}
        forget_past([session(2, at(13, 0))], NOW, sent, TZ)
        assert sent == {(2, 60)}


class TestFormatReminder:
    def test_names_the_course_and_the_start_time(self) -> None:
        text = format_reminder({"courseName": "Mathe"}, 10, NOW.replace(hour=13, minute=0))
        assert text == "In 10 minutes: Mathe at 13:00"

    def test_uses_the_singular_for_one_minute(self) -> None:
        assert "In 1 minute:" in format_reminder({"courseName": "Mathe"}, 1, NOW)

    def test_includes_the_topic_when_there_is_one(self) -> None:
        text = format_reminder({"courseName": "Mathe", "topic": "Integrale"}, 5, NOW)
        assert text.endswith("- Integrale")

    def test_falls_back_when_the_course_has_no_name(self) -> None:
        assert "Study session" in format_reminder({}, 5, NOW)


class TestReminderLeads:
    def _settings(self, value: str) -> Settings:
        return Settings(
            telegram_bot_token="t",
            telegram_webhook_secret="s",
            telegram_allowed_chat_ids="1",
            studylife_base_url="https://example.invalid",
            studylife_api_key="k",
            session_reminder_minutes=value,
        )

    def test_parses_the_studylife_default_largest_first(self) -> None:
        assert self._settings("60,30,10,5,3,2,1").reminder_leads == (60, 30, 10, 5, 3, 2, 1)

    def test_drops_junk_rather_than_raising(self) -> None:
        assert self._settings("30, nonsense, -5, 10, 10").reminder_leads == (30, 10)

    def test_empty_switches_reminders_off(self) -> None:
        assert self._settings("").reminder_leads == ()
