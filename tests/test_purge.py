import hashlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.fernet import Fernet

from studylife_telegram import main
from studylife_telegram.commands import RunTracker
from studylife_telegram.config import Settings
from studylife_telegram.store import LinkStore, PendingLink, token_fingerprint

INSTANCE = "https://studylife.example"
SECRET = "purge-secret"
HEADER = {"X-StudyLife-Shared-Secret": SECRET}


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def settings(purge_secret: str | None) -> Settings:
    return Settings(
        telegram_bot_token="t",
        telegram_webhook_secret="w",
        telegram_allow_any_chat=True,
        studylife_base_url=INSTANCE,
        link_encryption_key=Fernet.generate_key().decode(),
        studylife_purge_secret=purge_secret,
    )


@pytest.fixture
async def store(tmp_path) -> AsyncIterator[LinkStore]:
    store = LinkStore(str(tmp_path / "links.db"), Fernet.generate_key().decode())
    await store.open()
    yield store
    await store.close()


@pytest.fixture
def runs() -> RunTracker:
    return RunTracker()


@pytest.fixture
async def client(store, runs, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    # No lifespan: the route only needs the store and the run tracker on app.state.
    monkeypatch.setattr(main, "get_settings", lambda: settings(SECRET))
    main.app.state.store = store
    main.app.state.runs = runs
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot") as http:
        yield http


async def seed(store: LinkStore) -> None:
    await store.link(1, "key-alice", INSTANCE, "7")
    await store.link(2, "key-bob", INSTANCE, "8")
    # Same user id on a different instance: ids are not unique across instances.
    await store.link(3, "key-other-instance", "https://other.example", "7")
    await store.put_confirmation(4, "12345678", "key-alice-new", INSTANCE, "7")
    await store.put_confirmation(5, "87654321", "key-carol", INSTANCE, "9")
    await store.put_pending(
        PendingLink(state="s1", chat_id=1, code_verifier="v", instance_url=INSTANCE)
    )
    await store.put_pending(
        PendingLink(state="s2", chat_id=2, code_verifier="v", instance_url=INSTANCE)
    )


class TestPurgeRoute:
    async def test_without_a_configured_secret_the_route_is_off(self, client, monkeypatch):
        monkeypatch.setattr(main, "get_settings", lambda: settings(None))
        response = await client.post("/internal/users/7/purge", json={}, headers=HEADER)
        assert response.status_code == 503

    async def test_an_empty_secret_is_off_too(self, client, monkeypatch):
        monkeypatch.setattr(main, "get_settings", lambda: settings(""))
        response = await client.post("/internal/users/7/purge", json={}, headers={})
        assert response.status_code == 503

    @pytest.mark.parametrize("headers", [{}, {"X-StudyLife-Shared-Secret": "wrong"}])
    async def test_a_missing_or_wrong_secret_is_401(self, client, store, headers):
        await seed(store)
        response = await client.post(
            "/internal/users/7/purge", json={"key_hashes": [digest("key-alice")]}, headers=headers
        )
        assert response.status_code == 401
        assert await store.get(1) is not None

    async def test_unauthenticated_beats_validation(self, client):
        # A probe must learn nothing about the body schema.
        response = await client.post("/internal/users/7/purge", json={"key_hashes": "x"})
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "body",
        [
            {"key_hashes": ["not-a-hash"]},
            {"key_hashes": [digest("k").upper()]},
            {"key_hashes": [digest("k")[:63]]},
            {"key_hashes": [digest(str(i)) for i in range(51)]},
            {"key_hashes": "x"},
        ],
    )
    async def test_a_malformed_body_is_422(self, client, body):
        response = await client.post("/internal/users/99/purge", json=body, headers=HEADER)
        assert response.status_code == 422

    @pytest.mark.parametrize("user_id", ["0", "-3", "abc"])
    async def test_a_bad_user_id_is_422(self, client, user_id):
        response = await client.post(f"/internal/users/{user_id}/purge", json={}, headers=HEADER)
        assert response.status_code == 422

    async def test_fifty_hashes_are_accepted(self, client):
        body = {"key_hashes": [digest(str(i)) for i in range(50)]}
        response = await client.post("/internal/users/99/purge", json=body, headers=HEADER)
        assert response.status_code == 204

    async def test_deletes_only_the_matching_rows_and_is_idempotent(self, client, store, runs):
        await seed(store)
        started = datetime.now(UTC)
        runs.start(1, 10, started)
        runs.start(2, 11, started)
        body = {"key_hashes": [digest("key-alice"), digest("key-alice-new")]}

        first = await client.post("/internal/users/99/purge", json=body, headers=HEADER)
        assert first.status_code == 204
        assert first.content == b""

        # Alice's link, her waiting confirmation and her chat's open /login are gone.
        assert await store.get(1) is None
        assert await store.get_confirmation(4) is None
        assert await store.take_pending("s1") is None
        assert runs.finish(1) is None
        # Everyone else, including the same user id on another instance, is untouched.
        assert (await store.get(2)).api_key == "key-bob"  # type: ignore[union-attr]
        assert (await store.get(3)).api_key == "key-other-instance"  # type: ignore[union-attr]
        assert (await store.get_confirmation(5)).api_key == "key-carol"  # type: ignore[union-attr]
        assert await store.take_pending("s2") is not None
        assert runs.finish(2) is not None

        second = await client.post("/internal/users/99/purge", json=body, headers=HEADER)
        assert second.status_code == 204
        assert (await store.get(2)) is not None

    async def test_no_body_and_unknown_hashes_are_a_204_that_changes_nothing(self, client, store):
        await seed(store)
        assert (await client.post("/internal/users/99/purge", headers=HEADER)).status_code == 204
        body = {"key_hashes": [digest("never-issued")]}
        assert (
            await client.post("/internal/users/99/purge", json=body, headers=HEADER)
        ).status_code == 204
        assert len(await store.all_links()) == 3

    async def test_logs_counts_and_never_chat_ids_or_hashes(self, client, store, caplog):
        await store.link(424242, "key-alice", INSTANCE, "7")
        caplog.set_level(logging.INFO)  # aiosqlite logs its SQL parameters at DEBUG
        body = {"key_hashes": [digest("key-alice")]}
        await client.post("/internal/users/7/purge", json=body, headers=HEADER)
        text = caplog.text
        assert "Purged account 7: 1 links" in text
        assert "424242" not in text
        assert digest("key-alice") not in text


class TestPurgeByKeyHashes:
    async def test_a_failure_rolls_everything_back(self, store, monkeypatch):
        await seed(store)

        original = store._conn.execute

        async def boom() -> None:
            raise RuntimeError("disk")

        def flaky(sql, *args):
            # Fails on the link delete, after the pending login was already deleted.
            if sql.startswith("DELETE FROM linked_accounts"):
                return boom()
            return original(sql, *args)

        monkeypatch.setattr(store._conn, "execute", flaky, raising=False)
        with pytest.raises(RuntimeError):
            await store.purge_by_key_hashes(frozenset({digest("key-alice")}))
        monkeypatch.undo()
        # The pending login deleted earlier in the same transaction is back.
        assert await store.take_pending("s1") is not None
        assert await store.get(1) is not None

    async def test_hash_matches_the_servers_digest(self) -> None:
        assert token_fingerprint("abc") == hashlib.sha256(b"abc").hexdigest()

    async def test_unreadable_rows_are_skipped_not_fatal(self, tmp_path) -> None:
        path = str(tmp_path / "links.db")
        old = LinkStore(path, Fernet.generate_key().decode())
        await old.open()
        await old.link(1, "key-alice", INSTANCE, None)
        await old.close()
        rotated = LinkStore(path, Fernet.generate_key().decode())
        await rotated.open()
        try:
            outcome = await rotated.purge_by_key_hashes(frozenset({digest("key-alice")}))
            assert outcome.links == 0
        finally:
            await rotated.close()


class TestPurgeByUserId:
    """A link whose key was revoked earlier is unreachable by key hash, because the server no
    longer knows the key. The user id on the calling instance still finds it."""

    async def test_deletes_a_link_whose_key_was_revoked_before_the_deletion(self, client, store):
        await seed(store)
        # No hashes at all: the server has nothing left to send for user 7.
        response = await client.post(
            "/internal/users/7/purge", json={"key_hashes": []}, headers=HEADER
        )
        assert response.status_code == 204
        assert await store.get(1) is None  # user 7 on the calling instance
        assert await store.get_confirmation(4) is None
        assert await store.take_pending("s1") is None
        # Same id on another instance, and other users, stay.
        assert await store.get(3) is not None
        assert await store.get(2) is not None
        assert await store.get_confirmation(5) is not None

    async def test_deletes_a_link_whose_key_can_no_longer_be_decrypted(self, tmp_path):
        path = str(tmp_path / "links.db")
        old = LinkStore(path, Fernet.generate_key().decode())
        await old.open()
        await old.link(1, "key-alice", INSTANCE, "7")
        await old.close()
        rotated = LinkStore(path, Fernet.generate_key().decode())
        await rotated.open()
        try:
            outcome = await rotated.purge_by_key_hashes(
                frozenset(), user_id="7", instance_url=INSTANCE
            )
            assert outcome.links == 1
        finally:
            await rotated.close()

    async def test_the_id_alone_is_never_enough(self, store):
        await seed(store)
        outcome = await store.purge_by_key_hashes(frozenset(), user_id="7")
        assert outcome.links == 0
        assert await store.get(1) is not None
