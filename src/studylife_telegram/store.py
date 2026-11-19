"""Which Telegram chat is connected to which StudyAtrium account.

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

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from .confirmation import CONFIRMATION_TTL_SECONDS

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
CREATE TABLE IF NOT EXISTS pending_confirmations (
    chat_id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    encrypted_api_key BLOB NOT NULL,
    instance_url TEXT NOT NULL,
    studylife_user_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
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


@dataclass(frozen=True)
class PendingConfirmation:
    """An approved login waiting for its code to be typed into the chat. Not linked yet."""

    chat_id: int
    code: str
    api_key: str
    instance_url: str
    studylife_user_id: str | None
    attempts: int
    created_at: float


@dataclass(frozen=True)
class PurgeOutcome:
    """What an account purge removed. `chat_ids` is for the caller's in-memory bookkeeping only
    and must never be logged; the counts are what may appear in a log line."""

    chat_ids: frozenset[int]
    links: int
    confirmations: int
    pending_links: int


def token_fingerprint(token: str) -> str:
    """Lookup fingerprint of an issued token: lowercase hex SHA-256, the same digest as
    StudyAtrium's AuthSessionService. It is an identifier the server already sends us, not a
    password hash; the tokens are high-entropy random values."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
        await self.sweep_expired()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("LinkStore.open() was never awaited")
        return self._db

    async def sweep_expired(self, now: float | None = None) -> int:
        """Deletes open /login requests and unconfirmed approvals older than their lifetime and
        returns how many rows went. Without this, a login that is never completed (the link is
        never opened, the code never typed) would sit in the file indefinitely: expiry on its
        own only stops a row from working, it does not remove it. Runs when the store opens and
        whenever a new login or approval is stored, so it needs no timer of its own."""
        now = time.time() if now is None else now
        removed = 0
        for table, ttl in (
            ("pending_links", PENDING_TTL_SECONDS),
            ("pending_confirmations", CONFIRMATION_TTL_SECONDS),
        ):
            # `table` is one of two internal literals, never caller input.
            cursor = await self._conn.execute(
                f"DELETE FROM {table} WHERE created_at < ?", (now - ttl,)
            )
            removed += cursor.rowcount
        await self._conn.commit()
        return removed

    # --- the /login round trip ------------------------------------------------------------

    async def put_pending(self, pending: PendingLink) -> None:
        await self.sweep_expired()
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

    # --- the confirmation step (confirmation.py decides, this persists) -------------------

    async def put_confirmation(
        self,
        chat_id: int,
        code: str,
        api_key: str,
        instance_url: str,
        user_id: str | None,
        now: float | None = None,
    ) -> None:
        """One per chat: a newer approval replaces an older one, so only the latest code works.
        The API key is encrypted here too - it is a live credential while it waits."""
        await self.sweep_expired(now)
        await self._conn.execute(
            "INSERT OR REPLACE INTO pending_confirmations "
            "(chat_id, code, encrypted_api_key, instance_url, studylife_user_id, attempts, "
            "created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (
                chat_id,
                code,
                self._fernet.encrypt(api_key.encode("utf-8")),
                instance_url,
                user_id,
                time.time() if now is None else now,
            ),
        )
        await self._conn.commit()

    async def get_confirmation(self, chat_id: int) -> PendingConfirmation | None:
        async with self._conn.execute(
            "SELECT code, encrypted_api_key, instance_url, studylife_user_id, attempts, "
            "created_at FROM pending_confirmations WHERE chat_id = ?",
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            api_key = self._fernet.decrypt(row[1]).decode("utf-8")
        except InvalidToken:
            await self.discard_confirmation(chat_id)
            return None
        return PendingConfirmation(chat_id, row[0], api_key, row[2], row[3], row[4], row[5])

    async def record_wrong_attempt(self, chat_id: int) -> None:
        await self._conn.execute(
            "UPDATE pending_confirmations SET attempts = attempts + 1 WHERE chat_id = ?",
            (chat_id,),
        )
        await self._conn.commit()

    async def discard_confirmation(self, chat_id: int) -> None:
        await self._conn.execute("DELETE FROM pending_confirmations WHERE chat_id = ?", (chat_id,))
        await self._conn.commit()

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

    # --- account deletion -----------------------------------------------------------------

    async def purge_by_key_hashes(
        self,
        key_hashes: frozenset[str],
        user_id: str | None = None,
        instance_url: str | None = None,
    ) -> PurgeOutcome:
        """Deletes everything held for the API keys with these SHA-256 hashes, in one
        transaction: the links, the approvals still waiting for their code, and any /login
        started from a chat that just lost its link.

        The key hash is the primary match. When `user_id` AND `instance_url` are both given, a
        row stored for that user id on that instance goes too: that is what removes a link whose
        key was revoked earlier (so the server no longer knows its hash) or whose key cannot be
        decrypted any more (rotated encryption key). User ids are only unique per StudyAtrium
        instance and this bot serves several, which is why the id alone is never enough.
        Matching nothing is not an error - the call is idempotent.
        """
        by_user = user_id is not None and instance_url is not None
        if not key_hashes and not by_user:
            return PurgeOutcome(frozenset(), 0, 0, 0)
        conn = self._conn
        try:
            link_chats = await self._chats_with_key_in("linked_accounts", key_hashes)
            confirmation_chats = await self._chats_with_key_in("pending_confirmations", key_hashes)
            if by_user:
                link_chats |= await self._chats_of_user("linked_accounts", user_id, instance_url)
                confirmation_chats |= await self._chats_of_user(
                    "pending_confirmations", user_id, instance_url
                )
            chats = link_chats | confirmation_chats
            pending_links = 0
            for chat_id in chats:
                cursor = await conn.execute(
                    "DELETE FROM pending_links WHERE chat_id = ?", (chat_id,)
                )
                pending_links += cursor.rowcount
            for chat_id in link_chats:
                await conn.execute("DELETE FROM linked_accounts WHERE chat_id = ?", (chat_id,))
            for chat_id in confirmation_chats:
                await conn.execute(
                    "DELETE FROM pending_confirmations WHERE chat_id = ?", (chat_id,)
                )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        return PurgeOutcome(
            chat_ids=frozenset(chats),
            links=len(link_chats),
            confirmations=len(confirmation_chats),
            pending_links=pending_links,
        )

    async def _chats_of_user(
        self, table: str, user_id: str | None, instance_url: str | None
    ) -> set[int]:
        # `table` is one of two internal literals, never caller input.
        async with self._conn.execute(
            f"SELECT chat_id FROM {table} WHERE studylife_user_id = ? AND instance_url = ?",
            (user_id, instance_url),
        ) as cursor:
            return {row[0] for row in await cursor.fetchall()}

    async def links_for_user(self, user_id: str) -> list[LinkedAccount]:
        """Every chat linked to this StudyAtrium user id, for delivering one user's events to
        that user's own chats only. Ids are unique per instance, not across instances - see
        README.md ("Webhook deliveries") for what that means for a multi-instance deployment."""
        async with self._conn.execute(
            "SELECT chat_id, encrypted_api_key, instance_url, studylife_user_id "
            "FROM linked_accounts WHERE studylife_user_id = ? ORDER BY chat_id",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        accounts = [self._row_to_account(row[0], row[1:]) for row in rows]
        return [account for account in accounts if account is not None]

    async def _chats_with_key_in(self, table: str, key_hashes: frozenset[str]) -> set[int]:
        # `table` is one of two internal literals, never caller input.
        async with self._conn.execute(f"SELECT chat_id, encrypted_api_key FROM {table}") as cursor:
            rows = await cursor.fetchall()
        matched = set()
        for chat_id, encrypted in rows:
            try:
                token = self._fernet.decrypt(encrypted).decode("utf-8")
            except InvalidToken:
                continue
            if token_fingerprint(token) in key_hashes:
                matched.add(chat_id)
        return matched

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
