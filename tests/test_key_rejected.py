"""A 401 from the server means the stored key is dead for good: the link is removed."""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet

from studylife_telegram import main
from studylife_telegram.commands import ParsedCommand, RunTracker
from studylife_telegram.link_service import KEY_REJECTED_NOTICE
from studylife_telegram.reminders import ReminderLoop
from studylife_telegram.store import LinkStore
from studylife_telegram.studylife_client import KeyRejectedError, StudyLifeApiError

INSTANCE = "https://studylife.example"


@pytest.fixture
async def store(tmp_path) -> AsyncIterator[LinkStore]:
    store = LinkStore(str(tmp_path / "links.db"), Fernet.generate_key().decode())
    await store.open()
    await store.link(1, "key-alice", INSTANCE, "7")
    await store.link(2, "key-bob", INSTANCE, "8")
    yield store
    await store.close()


class TestCommandPath:
    def _app(self, store: LinkStore) -> SimpleNamespace:
        state = SimpleNamespace(store=store, runs=RunTracker(), tz=ZoneInfo("UTC"))
        return SimpleNamespace(state=state)

    async def test_a_rejected_key_removes_the_link_and_says_so(self, store, monkeypatch):
        monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace())

        async def rejected(*args, **kwargs):
            raise KeyRejectedError("revoked")

        monkeypatch.setattr(main, "handle", rejected)
        reply = await main._run_command(self._app(store), ParsedCommand("status", ""), 1)
        assert reply == KEY_REJECTED_NOTICE
        assert await store.get(1) is None
        assert await store.get(2) is not None

    async def test_other_api_errors_keep_the_link(self, store, monkeypatch):
        monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace())

        async def refused(*args, **kwargs):
            return "StudyAtrium refused that (403)."

        monkeypatch.setattr(main, "handle", refused)
        await main._run_command(self._app(store), ParsedCommand("status", ""), 1)
        assert await store.get(1) is not None


class TestReminderPath:
    def _loop(self, fetch, rejected: list[int]) -> ReminderLoop:
        async def on_rejected(chat_id: int) -> None:
            rejected.append(chat_id)

        async def accounts():
            return []

        async def send(chat_id: int, text: str) -> None:
            return None

        return ReminderLoop(
            accounts, fetch, send, ZoneInfo("UTC"), (5,), on_key_rejected=on_rejected
        )

    async def test_a_rejected_key_drops_the_chat_and_reports_it_once(self):
        async def fetch(account):
            raise KeyRejectedError("revoked")

        rejected: list[int] = []
        loop = self._loop(fetch, rejected)
        loop._sessions[1] = [{"id": 1}]
        await loop._refresh_one(SimpleNamespace(chat_id=1))
        assert rejected == [1]
        assert 1 not in loop._sessions

    async def test_a_transient_error_keeps_the_previous_list_and_the_link(self):
        async def fetch(account):
            raise StudyLifeApiError(502, "bad gateway")

        rejected: list[int] = []
        loop = self._loop(fetch, rejected)
        loop._sessions[1] = [{"id": 1}]
        await loop._refresh_one(SimpleNamespace(chat_id=1))
        assert rejected == []
        assert loop._sessions[1] == [{"id": 1}]
