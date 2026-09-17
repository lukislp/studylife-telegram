"""Which Telegram chat is connected to which StudyLife account.

Telegram, unlike Alexa, hands the bot no token on an incoming update - only a chat id. Alexa's
skill can therefore stay stateless and resolve its per-request access token to a key
(studylife-alexa's oauth_store); here the mapping has to be kept, so this is the one part of the
bot that owns state.

Shape follows studylife-mcp's oauth_store: a plain SQLite file on a small RWO volume, with the
API keys Fernet-encrypted at rest. Encrypting them inside the database is not theatre - the
volume is backed up by Velero to off-site object storage, and a backup of this file would
otherwise be a list of live credentials for every connected account.

Single replica by construction: SQLite over an RWO volume cannot be shared, which is why
k8s/02-app.yaml pins replicas to one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

_SCHEMA = """
CREATE TABLE IF NOT EXISTS linked_accounts (
    chat_id INTEGER PRIMARY KEY,
    encrypted_api_key BLOB NOT NULL,
    instance_url TEXT NOT NULL,
    studylife_user_id TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_links (
    state TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    code_verifier TEXT NOT NULL,
    instance_url TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

# How long a /login has to be completed in. The state is the only thing binding a public callback
# to a chat, so it is short-lived and single-use; anything longer widens the window in which a
# leaked link could be replayed.
PENDING_TTL_SECONDS = 600.0


@dataclass(frozen=True)
class LinkedAccount:
    chat_id: int
    api_key: str
    instance_url: str
    studylife_user_id: str | None


@dataclass(frozen=True)
class PendingLink:
    state: str
    chat_id: int
    code_verifier: str
    instance_url: str


class LinkStore:
    def __init__(self, db_path: str, encryption_key: str) -> None:
        self._db_path = db_path
        self._fernet = Fernet(encryption_key.encode("ascii"))
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        # WAL so a reader (the reminder loop) never blocks a writer (a callback landing).
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("LinkStore.open() was never awaited")
        return self._db

    # --- the /login round trip ------------------------------------------------------------

    async def put_pending(self, pending: PendingLink) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO pending_links "
            "(state, chat_id, code_verifier, instance_url, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                pending.state,
                pending.chat_id,
                pending.code_verifier,
                pending.instance_url,
                time.time(),
            ),
        )
        await self._conn.commit()

    async def take_pending(self, state: str, now: float | None = None) -> PendingLink | None:
        """Consumes a pending login. Single use and deleted whether or not it was still valid,
        so a state that leaked out of a chat cannot be replayed even within its TTL."""
        now = time.time() if now is None else now
        async with self._conn.execute(
            "SELECT chat_id, code_verifier, instance_url, created_at FROM pending_links "
            "WHERE state = ?",
            (state,),
        ) as cursor:
            row = await cursor.fetchone()
        await self._conn.execute("DELETE FROM pending_links WHERE state = ?", (state,))
        await self._conn.execute(
            "DELETE FROM pending_links WHERE created_at < ?", (now - PENDING_TTL_SECONDS,)
        )
        await self._conn.commit()
        if row is None or row[3] < now - PENDING_TTL_SECONDS:
            return None
        return PendingLink(state=state, chat_id=row[0], code_verifier=row[1], instance_url=row[2])

    # --- the link itself ------------------------------------------------------------------

    async def link(
        self, chat_id: int, api_key: str, instance_url: str, user_id: str | None
    ) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO linked_accounts "
            "(chat_id, encrypted_api_key, instance_url, studylife_user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                chat_id,
                self._fernet.encrypt(api_key.encode("utf-8")),
                instance_url,
                user_id,
                time.time(),
            ),
        )
        await self._conn.commit()

    async def unlink(self, chat_id: int) -> bool:
        cursor = await self._conn.execute(
            "DELETE FROM linked_accounts WHERE chat_id = ?", (chat_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def get(self, chat_id: int) -> LinkedAccount | None:
        async with self._conn.execute(
            "SELECT encrypted_api_key, instance_url, studylife_user_id FROM linked_accounts "
            "WHERE chat_id = ?",
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_account(chat_id, row)

    async def all_links(self) -> list[LinkedAccount]:
        """Every connected account, for the reminder loop. Ordered so a log of what was polled
        reads the same across restarts."""
        async with self._conn.execute(
            "SELECT chat_id, encrypted_api_key, instance_url, studylife_user_id "
            "FROM linked_accounts ORDER BY chat_id"
        ) as cursor:
            rows = await cursor.fetchall()
        accounts = []
        for row in rows:
            account = self._row_to_account(row[0], row[1:])
            if account is not None:
                accounts.append(account)
        return accounts

    def _row_to_account(self, chat_id: int, row: Any) -> LinkedAccount | None:
        if row is None:
            return None
        try:
            api_key = self._fernet.decrypt(row[0]).decode("utf-8")
        except InvalidToken:
            # The encryption key was rotated or lost. Treated as "not connected" rather than
            # raised: the user can simply /login again, whereas an exception here would break
            # every command for everyone.
            return None
        return LinkedAccount(
            chat_id=chat_id,
            api_key=api_key,
            instance_url=row[1],
            studylife_user_id=row[2],
        )
