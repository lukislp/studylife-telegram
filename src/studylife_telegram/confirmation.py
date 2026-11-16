"""The last step of /login: a code typed into the chat that started it.

Why it exists. The consent link is bound to the chat only by its single-use state, and a link can
be forwarded. Without a second step, an attacker could start /login in THEIR chat, forward the
link to a victim, and the victim's honest sign-in and approval would attach the victim's account
to the ATTACKER's chat. So after the approval the bot does not link anything yet: the browser
page shows a short code, and the account is attached only once that exact code arrives from the
chat that started the flow. A victim who was sent a forwarded link sees the code on their own
screen and types it into their own chat with the bot, where nothing is pending - it is useless
to the attacker, who never sees the page.

This module is pure: no I/O, no clock of its own. The store persists the pending confirmation,
main.py wires it to Telegram and the callback page.
"""

from __future__ import annotations

import enum
import re
import secrets
from dataclasses import dataclass

CODE_DIGITS = 8
# Ten minutes, like the consent state: long enough to switch from the browser back to the chat,
# short enough that a code left on a screen goes stale.
CONFIRMATION_TTL_SECONDS = 600.0
MAX_ATTEMPTS = 5

_SEPARATORS = re.compile(r"[\s-]")
_CODE_SHAPED = re.compile(rf"^\d{{4}}[\s-]?\d{{{CODE_DIGITS - 4}}}$")


def new_code() -> str:
    """A uniformly random code from the OS CSPRNG, zero-padded to CODE_DIGITS digits."""
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def format_code(code: str) -> str:
    """For display: 1234 5678. Easier to read and to type than one run of digits."""
    half = CODE_DIGITS // 2
    return f"{code[:half]} {code[half:]}"


def normalise(supplied: str) -> str:
    """Drops the spaces and dashes people (and the display format) put inside a code."""
    return _SEPARATORS.sub("", supplied)


def looks_like_code(text: str | None) -> bool:
    """Whether a plain (non-command) message is shaped like a confirmation code, so that pasting
    just the digits works as well as /confirm <code>."""
    return text is not None and _CODE_SHAPED.match(text.strip()) is not None


class Verdict(enum.Enum):
    CONFIRMED = "confirmed"
    WRONG = "wrong"  # more attempts are allowed
    LOCKED = "locked"  # too many wrong attempts - the pending link must be discarded
    EXPIRED = "expired"


@dataclass(frozen=True)
class Evaluation:
    verdict: Verdict
    attempts_left: int = 0


def evaluate(
    expected: str, supplied: str, attempts_so_far: int, created_at: float, now: float
) -> Evaluation:
    """Decides one attempt. Expiry wins over everything, then the attempt cap, then the compare.

    The comparison is constant-time over the normalised input, so how many leading digits were
    right is not observable. `attempts_so_far` counts wrong tries already recorded; the caller
    persists the outcome and discards the pending link on anything but WRONG.
    """
    if now - created_at > CONFIRMATION_TTL_SECONDS:
        return Evaluation(Verdict.EXPIRED)
    if attempts_so_far >= MAX_ATTEMPTS:
        return Evaluation(Verdict.LOCKED)
    if secrets.compare_digest(normalise(supplied).encode(), expected.encode()):
        return Evaluation(Verdict.CONFIRMED)
    left = MAX_ATTEMPTS - attempts_so_far - 1
    if left <= 0:
        return Evaluation(Verdict.LOCKED)
    return Evaluation(Verdict.WRONG, attempts_left=left)
