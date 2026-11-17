"""Account deletion on the StudyAtrium side reaches this bot as a purge call (GDPR Art. 17).

The server sends the SHA-256 hashes of every API key it issued to this bot for the deleted
account; everything this bot holds for those keys goes with them. Kept free of FastAPI routing so
the rules can be tested against a real store, with main.py as a thin adapter.
"""

from __future__ import annotations

import logging
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from .commands import RunTracker
from .store import LinkStore

logger = logging.getLogger(__name__)

MAX_KEY_HASHES = 50

KeyHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class PurgeRequest(BaseModel):
    key_hashes: list[KeyHash] = Field(default_factory=list, max_length=MAX_KEY_HASHES)


async def purge_account(
    store: LinkStore, runs: RunTracker, user_id: int, request: PurgeRequest
) -> None:
    """Removes every trace of the deleted account. Idempotent: matching nothing is success."""
    outcome = await store.purge_by_key_hashes(frozenset(request.key_hashes))
    for chat_id in outcome.chat_ids:
        # A timer run in progress must not be logged to an account that no longer exists. The
        # reminder loop drops its per-chat state itself on its next pass, because the link is
        # gone from the store.
        runs.forget(chat_id)
    # Counts only: chat ids and key hashes identify a person and stay out of the log.
    logger.info(
        "Purged account %d: %d links, %d pending confirmations, %d pending logins",
        user_id,
        outcome.links,
        outcome.confirmations,
        outcome.pending_links,
    )
