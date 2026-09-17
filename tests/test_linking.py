import base64
import hashlib

import pytest

from studylife_telegram.config import Settings
from studylife_telegram.linking import (
    build_connect_url,
    callback_url,
    instance_allowed,
    is_private_chat,
    new_pkce_pair,
    new_state,
    normalise_instance,
)

INSTANCE = "https://studylife.example"
ALLOWED = frozenset({INSTANCE})


class TestPkce:
    def test_challenge_is_the_unpadded_base64url_sha256_of_the_verifier(self) -> None:
        # The server validates S256 exactly; a padded or hex digest is rejected there, not here.
        verifier, challenge = new_pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        assert challenge == expected
        assert "=" not in challenge

    def test_every_pair_is_different(self) -> None:
        assert new_pkce_pair()[0] != new_pkce_pair()[0]

    def test_state_has_real_entropy(self) -> None:
        # The state is the ONLY thing binding a public callback to a chat.
        states = {new_state() for _ in range(100)}
        assert len(states) == 100
        assert all(len(s) >= 40 for s in states)


class TestInstanceAllowed:
    def test_accepts_the_configured_instance(self) -> None:
        assert instance_allowed(INSTANCE, ALLOWED, allow_any=False) is True

    def test_ignores_a_trailing_slash(self) -> None:
        assert instance_allowed(INSTANCE + "/", ALLOWED, allow_any=False) is True

    def test_refuses_anything_not_configured(self) -> None:
        # Without this the bot would fetch and post to whatever host a stranger names, from
        # inside the cluster network.
        assert instance_allowed("https://attacker.example", ALLOWED, allow_any=False) is False

    def test_refuses_a_lookalike_host(self) -> None:
        assert (
            instance_allowed("https://studylife.example.attacker.test", ALLOWED, allow_any=False)
            is False
        )

    def test_refuses_plain_http_even_when_allowed_or_open(self) -> None:
        # http would put the API key on the wire in clear text.
        assert instance_allowed("http://studylife.example", ALLOWED, allow_any=False) is False
        assert instance_allowed("http://anything.example", ALLOWED, allow_any=True) is False
        assert instance_allowed("http://127.0.0.1:8000", ALLOWED, allow_any=True) is False

    def test_allow_any_opens_it_up_for_a_public_deployment(self) -> None:
        assert instance_allowed("https://someone-else.example", ALLOWED, allow_any=True) is True

    def test_refuses_junk(self) -> None:
        for value in ("", "   ", "not a url", "ftp://x.example", "//evil.example"):
            assert instance_allowed(value, ALLOWED, allow_any=True) is False


class TestConnectUrl:
    def test_carries_everything_the_consent_page_needs(self) -> None:
        url = build_connect_url(INSTANCE, "studylife-telegram", "https://bot.example/cb", "S", "C")
        assert url.startswith(f"{INSTANCE}/connect/client/studylife-telegram?")
        for part in (
            "redirect_uri=https%3A%2F%2Fbot.example%2Fcb",
            "state=S",
            "code_challenge=C",
            "code_challenge_method=S256",
        ):
            assert part in url

    def test_callback_url_is_the_exact_string_to_register(self) -> None:
        # The server matches registered redirect URIs character for character.
        assert callback_url("https://bot.example") == "https://bot.example/connect/callback"
        assert callback_url("https://bot.example/") == "https://bot.example/connect/callback"

    def test_normalise_strips_only_what_it_should(self) -> None:
        assert normalise_instance("  https://x.example/  ") == "https://x.example"


class TestPrivateChatOnly:
    def test_accepts_a_direct_chat(self) -> None:
        assert is_private_chat({"id": 1, "type": "private"}) is True

    def test_refuses_groups_channels_and_junk(self) -> None:
        # A group's chat id belongs to every member, including anyone added later.
        for chat in (
            {"id": -1, "type": "group"},
            {"id": -2, "type": "supergroup"},
            {"id": -3, "type": "channel"},
            {"id": 4},
            None,
            "private",
        ):
            assert is_private_chat(chat) is False


class TestAccessSwitches:
    def _settings(self, **overrides: object) -> Settings:
        base: dict[str, object] = {
            "telegram_bot_token": "t",
            "telegram_webhook_secret": "s",
            "telegram_allowed_chat_ids": "42",
            "studylife_base_url": INSTANCE,
            "link_encryption_key": "k",
        }
        base.update(overrides)
        return Settings(**base)  # type: ignore[arg-type]

    def test_the_configured_instance_is_always_allowed(self) -> None:
        assert self._settings().allowed_instances == frozenset({INSTANCE})

    def test_extra_instances_are_added_not_replaced(self) -> None:
        settings = self._settings(studylife_extra_instances="https://b.example/, https://c.example")
        assert settings.allowed_instances == frozenset(
            {INSTANCE, "https://b.example", "https://c.example"}
        )

    def test_opening_up_needs_its_own_flag_not_an_empty_allowlist(self) -> None:
        # An env var that silently goes missing must not be what opens the bot to the internet.
        assert self._settings(telegram_allowed_chat_ids="").allowed_chat_ids == frozenset()
        assert self._settings(telegram_allowed_chat_ids="").telegram_allow_any_chat is False
        assert self._settings(studylife_allow_any_instance=True).studylife_allow_any_instance

    @pytest.mark.parametrize("value", ["42", "42,43", " 42 , 43 "])
    def test_allowlist_parsing(self, value: str) -> None:
        assert 42 in self._settings(telegram_allowed_chat_ids=value).allowed_chat_ids
