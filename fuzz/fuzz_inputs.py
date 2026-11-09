"""Atheris fuzz harness for the untrusted-input parsers of the bot.

Contract under test, for text that arrives from Telegram chats, webhook headers and the
StudyLife API: parse_command() and parse_local() never raise and keep their documented shape,
instance_allowed() answers a bool and only ever allows https URLs, the two verify_* helpers
answer a bool for any header value (an exception would be a 500 instead of a 401), and
format_hours() turns any number into text instead of raising.

Run locally (Linux, needs the atheris wheel):
    uv sync --frozen --group fuzz
    uv run python fuzz/fuzz_inputs.py -max_total_time=60
CI runs the same harness for a short, fixed time budget (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import atheris

from studylife_telegram.commands import format_hours, parse_command
from studylife_telegram.linking import instance_allowed
from studylife_telegram.times import parse_local
from studylife_telegram.verify import (
    verify_studylife_signature,
    verify_telegram_secret,
    wants_announcement,
)


def test_one_input(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)

    text = fdp.ConsumeUnicodeNoSurrogates(128)
    command = parse_command(text)
    if command is not None:
        if not command.name or " " in command.name or "@" in command.name:
            raise AssertionError(f"parse_command({text!r}) produced a bad name {command.name!r}")
        if command.name != command.name.lower() or command.argument != command.argument.strip():
            raise AssertionError(f"parse_command({text!r}) did not normalise its result")
        if not text.strip().startswith("/"):
            raise AssertionError(f"parse_command({text!r}) accepted a non-command")

    parsed = parse_local(fdp.ConsumeUnicodeNoSurrogates(48), UTC)
    if parsed is not None and (not isinstance(parsed, datetime) or parsed.tzinfo is None):
        raise AssertionError("parse_local() returned a naive or non-datetime value")

    url = fdp.ConsumeUnicodeNoSurrogates(64)
    allowed = frozenset({fdp.ConsumeUnicodeNoSurrogates(32), "https://studylife.example"})
    if instance_allowed(url, allowed, fdp.ConsumeBool()) and not url.strip().lower().startswith(
        "https://"
    ):
        raise AssertionError(f"instance_allowed() accepted a non-https URL {url!r}")

    # Header values arrive latin-1 decoded, so non-ASCII text is a normal probe.
    secret = fdp.ConsumeUnicodeNoSurrogates(32)
    header = fdp.ConsumeUnicodeNoSurrogates(64)
    if verify_telegram_secret(secret, header) != (bool(secret) and secret == header):
        raise AssertionError("verify_telegram_secret() disagrees with plain equality")
    if not isinstance(verify_studylife_signature(secret, header, fdp.ConsumeBytes(64)), bool):
        raise AssertionError("verify_studylife_signature() did not return a bool")

    wants_announcement(fdp.ConsumeUnicodeNoSurrogates(24))
    if not isinstance(format_hours(fdp.ConsumeFloat()), str):
        raise AssertionError("format_hours() did not return text")


if __name__ == "__main__":
    # instrument_all() instead of instrument_imports(): the package is loaded through uv's
    # editable-install loader, which the import hook does not see (no coverage feedback).
    atheris.instrument_all()
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
