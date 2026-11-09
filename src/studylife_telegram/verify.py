"""Authentication for both inbound directions.

Telegram, unlike Discord, does not sign its webhook deliveries. The only proof a request really
came from Telegram is the secret token the bot itself chose when calling setWebhook, echoed back
in a header - so this module is the whole door, not one lock among several.

StudyLife's own outgoing webhooks DO sign, with HMAC-SHA256 over the raw body; see
studylife-webhooks/src/studylife_webhooks/delivery.py for the scheme this mirrors.
"""

from __future__ import annotations

import hashlib
import hmac

# Events this bot turns into a chat message. Everything else StudyLife may send is accepted
# (signature checked, 200 returned) and silently ignored, so adding an event type upstream never
# makes deliveries start failing here.
# Every name here exists in the server's own catalogue (WebhookEventTypes.cs). That is not a
# formality: "goal.due" used to sit in this set and can never arrive, because StudyLife has no
# such event - the branch was unreachable and nothing reported it.
_ANNOUNCED_EVENTS = frozenset(
    {
        "timer.started",
        "timer.ended",
        "session.completed",
        "course_goal.completed",
        "new_record.set",
        "plan.generated",
    }
)


def _equal(expected: str, received: str) -> bool:
    """Constant-time equality on bytes: hmac.compare_digest() raises TypeError for a str with
    non-ASCII characters, and header values are latin-1 decoded, so a probe can send them (a
    500 instead of a 401)."""
    return hmac.compare_digest(
        expected.encode("utf-8", "surrogatepass"), received.encode("utf-8", "surrogatepass")
    )


def verify_telegram_secret(expected: str, received: str | None) -> bool:
    """Constant-time comparison of the setWebhook secret token.

    A missing header is a plain False rather than an exception: an unauthenticated probe of a
    public endpoint is an ordinary event, not an error worth a stack trace.
    """
    if not received or not expected:
        return False
    return _equal(expected, received)


def verify_studylife_signature(secret: str, signature_hex: str | None, body: bytes) -> bool:
    """HMAC-SHA256 of the raw body, hex-encoded, compared in constant time.

    The RAW bytes matter: re-serialising the parsed JSON would reorder keys and change
    whitespace, and the signature would never match again.
    """
    if not signature_hex or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return _equal(expected, signature_hex)


def is_allowed_chat(allowed: frozenset[int], chat_id: int | None) -> bool:
    """Whether this chat may drive the account. See Settings.telegram_allowed_chat_ids for why
    an allowlist is not optional here."""
    return chat_id is not None and chat_id in allowed


def wants_announcement(event_type: object) -> bool:
    return isinstance(event_type, str) and event_type in _ANNOUNCED_EVENTS
