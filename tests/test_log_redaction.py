import logging

from studylife_telegram.main import RedactCallbackQuery


def record(*args: object) -> logging.LogRecord:
    """An access-log record shaped the way uvicorn builds one: the request line is an argument,
    not part of the formatted message."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=args,
        exc_info=None,
    )


class TestRedactCallbackQuery:
    def test_strips_the_assertion_from_the_request_line(self) -> None:
        rec = record(
            "10.0.0.1:1",
            "GET",
            "/connect/callback?assertion=SECRET-ASSERTION&state=SECRET-STATE",
            "1.1",
            200,
        )
        assert RedactCallbackQuery().filter(rec) is True
        assert "SECRET-ASSERTION" not in str(rec.args)
        assert "SECRET-STATE" not in str(rec.args)
        assert "/connect/callback?<redacted>" in str(rec.args)

    def test_leaves_other_paths_alone(self) -> None:
        # Over-broad redaction would make the log useless for everything else.
        rec = record("10.0.0.1:1", "POST", "/telegram", "1.1", 200)
        RedactCallbackQuery().filter(rec)
        assert "/telegram" in str(rec.args)

    def test_leaves_a_bare_callback_alone(self) -> None:
        rec = record("10.0.0.1:1", "GET", "/connect/callback", "1.1", 200)
        RedactCallbackQuery().filter(rec)
        assert "/connect/callback" in str(rec.args)
        assert "redacted" not in str(rec.args)

    def test_never_drops_a_record(self) -> None:
        # A filter returning False would silently delete access logging altogether.
        assert RedactCallbackQuery().filter(record()) is True

    def test_survives_a_record_whose_args_are_not_a_tuple(self) -> None:
        rec = record()
        rec.args = {"weird": "mapping"}  # type: ignore[assignment]
        assert RedactCallbackQuery().filter(rec) is True
