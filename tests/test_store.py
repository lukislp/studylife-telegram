import time

import pytest
from cryptography.fernet import Fernet

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
