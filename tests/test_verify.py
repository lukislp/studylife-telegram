import hashlib
import hmac

from studylife_telegram.verify import (
    is_allowed_chat,
    verify_studylife_signature,
    verify_telegram_secret,
    wants_announcement,
)


class TestTelegramSecret:
    def test_accepts_the_exact_token(self) -> None:
        assert verify_telegram_secret("s3cret-token", "s3cret-token") is True

    def test_rejects_a_wrong_missing_or_empty_token(self) -> None:
        assert verify_telegram_secret("s3cret-token", "other") is False
        assert verify_telegram_secret("s3cret-token", None) is False
        assert verify_telegram_secret("s3cret-token", "") is False

    def test_rejects_when_no_secret_is_configured(self) -> None:
        # Otherwise an empty expected value would authenticate an empty header, turning a
        # misconfiguration into an open door.
        assert verify_telegram_secret("", "") is False
        assert verify_telegram_secret("", "anything") is False

    def test_rejects_a_prefix_of_the_real_token(self) -> None:
        assert verify_telegram_secret("s3cret-token", "s3cret") is False


class TestStudyLifeSignature:
    secret = "webhook-secret"
    body = b'{"eventType":"timer.started","data":{"courseName":"Betriebssysteme"}}'

    def _sign(self, body: bytes, secret: str | None = None) -> str:
        return hmac.new((secret or self.secret).encode(), body, hashlib.sha256).hexdigest()

    def test_accepts_a_correct_signature(self) -> None:
        assert verify_studylife_signature(self.secret, self._sign(self.body), self.body) is True

    def test_rejects_a_signature_made_with_another_secret(self) -> None:
        wrong = self._sign(self.body, "someone-elses-secret")
        assert verify_studylife_signature(self.secret, wrong, self.body) is False

    def test_rejects_when_the_body_changed_by_one_byte(self) -> None:
        signature = self._sign(self.body)
        assert verify_studylife_signature(self.secret, signature, self.body + b" ") is False

    def test_rejects_a_missing_signature_or_secret(self) -> None:
        assert verify_studylife_signature(self.secret, None, self.body) is False
        assert verify_studylife_signature("", self._sign(self.body), self.body) is False

    def test_rejects_garbage_instead_of_raising(self) -> None:
        assert verify_studylife_signature(self.secret, "not-hex-at-all", self.body) is False


class TestChatAllowlist:
    def test_accepts_a_listed_chat(self) -> None:
        assert is_allowed_chat(frozenset({42, 99}), 42) is True

    def test_rejects_an_unlisted_or_missing_chat(self) -> None:
        assert is_allowed_chat(frozenset({42}), 7) is False
        assert is_allowed_chat(frozenset({42}), None) is False

    def test_rejects_everything_when_the_list_is_empty(self) -> None:
        assert is_allowed_chat(frozenset(), 42) is False


class TestAnnouncementFilter:
    def test_accepts_the_announced_events(self) -> None:
        for event in ("timer.started", "timer.ended", "session.completed", "goal.due"):
            assert wants_announcement(event) is True

    def test_ignores_anything_else_without_raising(self) -> None:
        assert wants_announcement("note.created") is False
        assert wants_announcement(None) is False
        assert wants_announcement(42) is False
