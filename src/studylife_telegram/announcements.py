"""Turns one signed StudyAtrium webhook delivery into a chat message for ONE user.

The delivery body is the one studylife-webhooks sends:

    {"event_type": "session.completed", "occurred_at": "...", "user_id": 7,
     "payload": {"courseName": "Analysis II", ...}}

`user_id` is part of the signed body, so the signature covers who the event belongs to. A delivery
without it is rejected: the signature alone proves only that SOME StudyAtrium sent the request,
and without an owner the only thing the bot could do is tell every connected chat about one
person's study activity.

Pure on purpose (no FastAPI, no store), so the rules are testable without a server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .verify import wants_announcement

_MAX_USER_ID_LENGTH = 64


@dataclass(frozen=True)
class Announcement:
    user_id: str
    text: str


class MissingUserError(ValueError):
    """The delivery names no usable owner, so it must not be delivered anywhere."""


def parse_user_id(payload: dict[str, Any]) -> str:
    """The owner as the string form the store keeps. Accepts a positive integer or a non-empty
    string; anything else (absent, null, a bool, a float, an object) raises MissingUserError."""
    raw = payload.get("user_id")
    if isinstance(raw, bool):
        raise MissingUserError("user_id is not a user id")
    if isinstance(raw, int):
        if raw > 0:
            return str(raw)
    elif isinstance(raw, str):
        value = raw.strip()
        if value and len(value) <= _MAX_USER_ID_LENGTH:
            return value
    raise MissingUserError("the delivery carries no usable user_id")


def parse_announcement(payload: dict[str, Any]) -> Announcement | None:
    """The announcement for this delivery, or None when it is an event the bot does not
    announce. Raises MissingUserError when the owner is missing - checked first, so an
    ownerless delivery is rejected whatever its event type is."""
    user_id = parse_user_id(payload)
    event = payload.get("event_type")
    if not wants_announcement(event):
        return None
    text = _announcement_text(str(event), payload.get("payload"))
    return Announcement(user_id, text) if text else None


def _announcement_text(event: str, data: object) -> str:
    fields = data if isinstance(data, dict) else {}
    course = fields.get("courseName")
    suffix = f" - {course}" if isinstance(course, str) and course else ""
    if event == "timer.started":
        return f"Focus session started{suffix}."
    if event == "timer.ended":
        return f"Focus session ended{suffix}."
    if event == "session.completed":
        return f"Session logged{suffix}."
    if event == "course_goal.completed":
        return f"Course goal completed{suffix}."
    if event == "new_record.set":
        return f"New record - longest session so far{suffix}."
    if event == "plan.generated":
        return "A new study plan was generated."
    return ""
