import hashlib
import hmac
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from cryptography.fernet import Fernet

from studylife_telegram import main
from studylife_telegram.announcements import MissingUserError, parse_announcement
from studylife_telegram.config import Settings
from studylife_telegram.store import LinkStore

INSTANCE = "https://studylife.example"
SECRET = "webhook-secret"


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def settings(secret: str | None = SECRET) -> Settings:
    return Settings(
        telegram_bot_token="t",
        telegram_webhook_secret="w",
        telegram_allow_any_chat=True,
        studylife_base_url=INSTANCE,
        link_encryption_key=Fernet.generate_key().decode(),
        studylife_webhook_secret=secret,
    )


def body(**fields: object) -> bytes:
    delivery: dict[str, object] = {
        "event_type": "session.completed",
        "occurred_at": "2026-11-19T12:00:00Z",
        "user_id": 7,
        "payload": {"courseName": "Analysis II"},
    }
    delivery.update(fields)
    return json.dumps({k: v for k, v in delivery.items() if v is not ...}).encode()


def sign(raw: bytes, secret: str = SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return {"X-StudyLife-Webhook-Signature": digest, "Content-Type": "application/json"}


@pytest.fixture
async def store(tmp_path) -> AsyncIterator[LinkStore]:
    store = LinkStore(str(tmp_path / "links.db"), Fernet.generate_key().decode())
    await store.open()
    await store.link(1, "key-alice", INSTANCE, "7")
    await store.link(2, "key-bob", INSTANCE, "8")
    await store.link(3, "key-no-id", INSTANCE, None)
    yield store
    await store.close()


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
async def client(store, telegram, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(main, "get_settings", lambda: settings())
    main.app.state.store = store
    main.app.state.telegram = telegram
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot") as http:
        yield http


async def post(client: httpx.AsyncClient, raw: bytes, headers: dict[str, str] | None = None):
    return await client.post(
        "/webhooks/studylife", content=raw, headers=sign(raw) if headers is None else headers
    )


class TestWebhookRoute:
    async def test_delivers_only_to_the_matching_users_chat(self, client, telegram):
        response = await post(client, body(user_id=7))
        assert response.status_code == 200
        assert telegram.sent == [(1, "Session logged - Analysis II.")]

    async def test_a_string_user_id_matches_too(self, client, telegram):
        await post(client, body(user_id="8"))
        assert [chat for chat, _ in telegram.sent] == [2]

    async def test_every_chat_of_the_same_user_gets_it(self, client, store, telegram):
        await store.link(4, "key-alice-2", INSTANCE, "7")
        await post(client, body(user_id=7))
        assert [chat for chat, _ in telegram.sent] == [1, 4]

    async def test_an_unknown_user_gets_no_delivery_and_no_error(self, client, telegram):
        response = await post(client, body(user_id=999))
        assert response.status_code == 200
        assert telegram.sent == []

    @pytest.mark.parametrize("user_id", [..., None, "", "   ", 0, -1, True, 1.5, [7], {"a": 1}])
    async def test_a_missing_or_unusable_user_id_is_rejected(self, client, telegram, user_id):
        response = await post(client, body(user_id=user_id))
        assert response.status_code == 400
        assert telegram.sent == []

    async def test_an_ownerless_unannounced_event_is_rejected_too(self, client, telegram):
        response = await post(client, body(user_id=..., event_type="note.created"))
        assert response.status_code == 400

    async def test_a_chat_without_a_stored_user_id_never_receives_anything(self, client, telegram):
        await post(client, body(user_id=7))
        assert 3 not in [chat for chat, _ in telegram.sent]

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"X-StudyLife-Webhook-Signature": "0" * 64},
        ],
    )
    async def test_a_bad_or_missing_signature_is_403_and_delivers_nothing(
        self, client, telegram, headers
    ):
        response = await post(client, body(user_id=7), headers=headers)
        assert response.status_code == 403
        assert telegram.sent == []

    async def test_a_signature_from_another_secret_is_403(self, client, telegram):
        raw = body(user_id=7)
        response = await post(client, raw, headers=sign(raw, "other-secret"))
        assert response.status_code == 403
        assert telegram.sent == []

    async def test_a_tampered_user_id_breaks_the_signature(self, client, telegram):
        original = body(user_id=7)
        tampered = original.replace(b'"user_id": 7', b'"user_id": 8')
        assert tampered != original
        response = await post(client, tampered, headers=sign(original))
        assert response.status_code == 403
        assert telegram.sent == []

    async def test_without_a_configured_secret_the_route_is_off(self, client, monkeypatch):
        monkeypatch.setattr(main, "get_settings", lambda: settings(None))
        assert (await post(client, body())).status_code == 404

    @pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'"x"'])
    async def test_a_signed_body_that_is_not_an_object_is_400(self, client, raw):
        assert (await post(client, raw)).status_code == 400

    async def test_an_event_the_bot_does_not_announce_is_a_200_without_delivery(
        self, client, telegram
    ):
        response = await post(client, body(user_id=7, event_type="note.created"))
        assert response.status_code == 200
        assert telegram.sent == []


class TestParseAnnouncement:
    def test_builds_the_text_for_the_owner(self):
        announcement = parse_announcement(json.loads(body(event_type="timer.started")))
        assert announcement is not None
        assert announcement.user_id == "7"
        assert announcement.text == "Focus session started - Analysis II."

    def test_a_missing_user_raises(self):
        with pytest.raises(MissingUserError):
            parse_announcement({"event_type": "session.completed"})
