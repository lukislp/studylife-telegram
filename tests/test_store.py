import time

import pytest
from cryptography.fernet import Fernet

from studylife_telegram.confirmation import CONFIRMATION_TTL_SECONDS
from studylife_telegram.store import PENDING_TTL_SECONDS, LinkStore, PendingLink

INSTANCE = "https://studylife.example"


@pytest.fixture
async def store(tmp_path):
    store = LinkStore(str(tmp_path / "links.db"), Fernet.generate_key().decode())
    await store.open()
    yield store
    await store.close()


class TestLinks:
    async def test_links_and_reads_back_one_chat(self, store: LinkStore) -> None:
        await store.link(42, "key-42", INSTANCE, "u1")
        account = await store.get(42)
        assert account is not None
        assert account.api_key == "key-42"
        assert account.instance_url == INSTANCE
        assert account.studylife_user_id == "u1"

    async def test_an_unknown_chat_is_simply_not_connected(self, store: LinkStore) -> None:
        assert await store.get(999) is None

    async def test_each_chat_keeps_its_own_account(self, store: LinkStore) -> None:
        # The whole point of the multi-user change: one process, many accounts, no bleed.
        await store.link(1, "key-one", INSTANCE, "u1")
        await store.link(2, "key-two", "https://other.example", "u2")
        first, second = await store.get(1), await store.get(2)
        assert first is not None and second is not None
        assert first.api_key == "key-one"
        assert second.api_key == "key-two"
        assert second.instance_url == "https://other.example"

    async def test_logging_in_again_replaces_the_key(self, store: LinkStore) -> None:
        await store.link(42, "old", INSTANCE, "u1")
        await store.link(42, "new", INSTANCE, "u1")
        account = await store.get(42)
        assert account is not None
        assert account.api_key == "new"

    async def test_unlink_reports_whether_anything_was_there(self, store: LinkStore) -> None:
        await store.link(42, "key", INSTANCE, None)
        assert await store.unlink(42) is True
        assert await store.unlink(42) is False
        assert await store.get(42) is None

    async def test_all_links_is_what_the_reminder_loop_iterates(self, store: LinkStore) -> None:
        await store.link(2, "b", INSTANCE, None)
        await store.link(1, "a", INSTANCE, None)
        assert [a.chat_id for a in await store.all_links()] == [1, 2]

    async def test_keys_are_not_stored_in_clear_text(self, tmp_path) -> None:
        # The volume is copied off-site by Velero; a backup must not be a credential list.
        path = tmp_path / "links.db"
        store = LinkStore(str(path), Fernet.generate_key().decode())
        await store.open()
        await store.link(42, "super-secret-key", INSTANCE, None)
        await store.close()
        blob = path.read_bytes()
        assert b"super-secret-key" not in blob

    async def test_a_rotated_encryption_key_reads_as_not_connected(self, tmp_path) -> None:
        # Rather than raising and breaking every command for everyone: the user can /login again.
        path = str(tmp_path / "links.db")
        first = LinkStore(path, Fernet.generate_key().decode())
        await first.open()
        await first.link(42, "key", INSTANCE, None)
        await first.close()

        second = LinkStore(path, Fernet.generate_key().decode())
        await second.open()
        assert await second.get(42) is None
        assert await second.all_links() == []
        await second.close()

    async def test_links_survive_a_restart(self, tmp_path) -> None:
        path = str(tmp_path / "links.db")
        key = Fernet.generate_key().decode()
        first = LinkStore(path, key)
        await first.open()
        await first.link(42, "key", INSTANCE, "u1")
        await first.close()

        second = LinkStore(path, key)
        await second.open()
        account = await second.get(42)
        assert account is not None
        assert account.api_key == "key"
        await second.close()


class TestPendingLogins:
    def _pending(self, state: str = "s1", chat_id: int = 42) -> PendingLink:
        return PendingLink(state=state, chat_id=chat_id, code_verifier="v", instance_url=INSTANCE)

    async def test_round_trip(self, store: LinkStore) -> None:
        await store.put_pending(self._pending())
        taken = await store.take_pending("s1")
        assert taken is not None
        assert taken.chat_id == 42
        assert taken.code_verifier == "v"

    async def test_is_single_use(self, store: LinkStore) -> None:
        # A state that leaked out of a chat must not be redeemable twice.
        await store.put_pending(self._pending())
        assert await store.take_pending("s1") is not None
        assert await store.take_pending("s1") is None

    async def test_expires(self, store: LinkStore) -> None:
        await store.put_pending(self._pending())
        later = time.time() + PENDING_TTL_SECONDS + 1
        assert await store.take_pending("s1", now=later) is None

    async def test_an_unknown_state_is_refused_rather_than_guessed(self, store: LinkStore) -> None:
        assert await store.take_pending("never-issued") is None

    async def test_taking_one_also_sweeps_expired_ones(self, store: LinkStore) -> None:
        await store.put_pending(self._pending("old", 1))
        await store.put_pending(self._pending("new", 2))
        later = time.time() + PENDING_TTL_SECONDS + 1
        assert await store.take_pending("new", now=later) is None
        # Both are gone: the sweep runs on every take, so the table cannot grow unbounded.
        assert await store.take_pending("old") is None

    async def test_a_second_login_replaces_the_first_for_that_chat(self, store: LinkStore) -> None:
        await store.put_pending(self._pending("s1"))
        await store.put_pending(self._pending("s2"))
        assert await store.take_pending("s2") is not None


class TestSweepExpired:
    async def _rows(self, store: LinkStore, table: str) -> int:
        async with store._conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    async def test_removes_expired_logins_and_approvals_that_nobody_completed(
        self, store: LinkStore
    ) -> None:
        now = time.time()
        await store.put_pending(
            PendingLink(state="old", chat_id=1, code_verifier="v", instance_url=INSTANCE)
        )
        await store.put_confirmation(2, "12345678", "key", INSTANCE, "u2", now=now)
        later = now + max(PENDING_TTL_SECONDS, CONFIRMATION_TTL_SECONDS) + 1

        assert await store.sweep_expired(now=later) == 2
        assert await self._rows(store, "pending_links") == 0
        assert await self._rows(store, "pending_confirmations") == 0

    async def test_keeps_what_is_still_valid_and_never_touches_links(
        self, store: LinkStore
    ) -> None:
        now = time.time()
        await store.link(1, "key", INSTANCE, "u1")
        await store.put_pending(
            PendingLink(state="fresh", chat_id=2, code_verifier="v", instance_url=INSTANCE)
        )
        await store.put_confirmation(3, "12345678", "key", INSTANCE, "u3", now=now)

        assert await store.sweep_expired(now=now + 1) == 0
        assert await store.get(1) is not None
        assert await self._rows(store, "pending_links") == 1
        assert await self._rows(store, "pending_confirmations") == 1

    async def test_storing_a_new_login_sweeps_the_expired_ones_first(
        self, store: LinkStore
    ) -> None:
        await store._conn.execute(
            "INSERT INTO pending_links (state, chat_id, code_verifier, instance_url, created_at) "
            "VALUES ('stale', 1, 'v', ?, ?)",
            (INSTANCE, time.time() - PENDING_TTL_SECONDS - 5),
        )
        await store._conn.commit()
        await store.put_pending(
            PendingLink(state="new", chat_id=2, code_verifier="v", instance_url=INSTANCE)
        )
        assert await self._rows(store, "pending_links") == 1

    async def test_opening_the_store_sweeps(self, tmp_path) -> None:
        path = str(tmp_path / "links.db")
        key = Fernet.generate_key().decode()
        first = LinkStore(path, key)
        await first.open()
        await first._conn.execute(
            "INSERT INTO pending_links (state, chat_id, code_verifier, instance_url, created_at) "
            "VALUES ('stale', 1, 'v', ?, ?)",
            (INSTANCE, time.time() - PENDING_TTL_SECONDS - 5),
        )
        await first._conn.commit()
        await first.close()

        second = LinkStore(path, key)
        await second.open()
        try:
            assert await self._rows(second, "pending_links") == 0
        finally:
            await second.close()


class TestLinksForUser:
    async def test_returns_only_that_users_chats(self, store: LinkStore) -> None:
        await store.link(1, "a", INSTANCE, "7")
        await store.link(2, "b", INSTANCE, "8")
        await store.link(3, "c", INSTANCE, None)
        await store.link(4, "d", INSTANCE, "7")
        assert [a.chat_id for a in await store.links_for_user("7", INSTANCE)] == [1, 4]
        assert await store.links_for_user("9", INSTANCE) == []

    async def test_the_same_user_id_on_another_instance_is_a_different_user(
        self, store: LinkStore
    ) -> None:
        await store.link(1, "a", INSTANCE, "7")
        await store.link(2, "b", "https://other.example", "7")
        assert [a.chat_id for a in await store.links_for_user("7", INSTANCE)] == [1]
        assert [a.chat_id for a in await store.links_for_user("7", "https://other.example")] == [2]
