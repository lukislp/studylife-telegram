import pytest
from cryptography.fernet import Fernet

from studylife_telegram.confirmation import (
    CODE_DIGITS,
    CONFIRMATION_TTL_SECONDS,
    MAX_ATTEMPTS,
    Verdict,
    evaluate,
    format_code,
    looks_like_code,
    new_code,
    normalise,
)
from studylife_telegram.link_service import begin_confirmation, confirm
from studylife_telegram.store import LinkStore, PendingLink

INSTANCE = "https://studylife.example"
ATTACKER, VICTIM = 111, 222


class TestPureRules:
    def test_codes_are_fixed_length_digits_and_not_repeated(self) -> None:
        codes = {new_code() for _ in range(200)}
        assert len(codes) > 190
        assert all(len(c) == CODE_DIGITS and c.isdigit() for c in codes)

    def test_display_format_round_trips_through_normalise(self) -> None:
        code = "01234567"
        assert format_code(code) == "0123 4567"
        assert normalise(format_code(code)) == code
        assert normalise("0123-4567") == code

    def test_looks_like_code(self) -> None:
        assert looks_like_code("1234 5678")
        assert looks_like_code(" 12345678 ")
        assert not looks_like_code("hello")
        assert not looks_like_code("123")
        assert not looks_like_code(None)

    def test_match_confirms(self) -> None:
        assert evaluate("12345678", "1234 5678", 0, 0.0, 1.0).verdict is Verdict.CONFIRMED

    def test_mismatch_counts_down_then_locks(self) -> None:
        first = evaluate("12345678", "00000000", 0, 0.0, 1.0)
        assert (first.verdict, first.attempts_left) == (Verdict.WRONG, MAX_ATTEMPTS - 1)
        assert evaluate("12345678", "0", MAX_ATTEMPTS - 1, 0.0, 1.0).verdict is Verdict.LOCKED

    def test_correct_code_after_the_cap_is_still_refused(self) -> None:
        assert evaluate("12345678", "12345678", MAX_ATTEMPTS, 0.0, 1.0).verdict is Verdict.LOCKED

    def test_expiry_beats_a_correct_code(self) -> None:
        late = CONFIRMATION_TTL_SECONDS + 1
        assert evaluate("12345678", "12345678", 0, 0.0, late).verdict is Verdict.EXPIRED


@pytest.fixture
async def store(tmp_path):
    store = LinkStore(str(tmp_path / "links.db"), Fernet.generate_key().decode())
    await store.open()
    yield store
    await store.close()


async def _approved(store: LinkStore, chat_id: int, key: str = "victim-key") -> str:
    pending = PendingLink("s", chat_id, "verifier", INSTANCE)
    return await begin_confirmation(store, pending, key, "victim-user")


class TestForwardedLinkAttack:
    async def test_approval_alone_links_nothing(self, store: LinkStore) -> None:
        # The attack: the victim approves a link the attacker's chat started.
        await _approved(store, ATTACKER)
        assert await store.get(ATTACKER) is None
        assert await store.get(VICTIM) is None

    async def test_the_victim_typing_the_code_into_their_own_chat_links_nothing(
        self, store: LinkStore
    ) -> None:
        code = await _approved(store, ATTACKER)
        result = await confirm(store, VICTIM, code)
        assert result.verdict is None
        assert await store.get(ATTACKER) is None
        assert await store.get(VICTIM) is None

    async def test_attacker_cannot_guess_the_code(self, store: LinkStore) -> None:
        code = await _approved(store, ATTACKER)
        wrong = "00000000" if code != "00000000" else "11111111"
        for _ in range(MAX_ATTEMPTS):
            assert (await confirm(store, ATTACKER, wrong)).verdict in {
                Verdict.WRONG,
                Verdict.LOCKED,
            }
        assert await store.get(ATTACKER) is None
        # Locked out: even the real code no longer works, the pending link is gone.
        assert (await confirm(store, ATTACKER, code)).verdict is None
        assert await store.get(ATTACKER) is None

    async def test_a_wrong_code_from_another_chat_does_not_burn_or_use_the_pending_link(
        self, store: LinkStore
    ) -> None:
        code = await _approved(store, ATTACKER)
        assert (await confirm(store, VICTIM, "00000000")).verdict is None
        waiting = await store.get_confirmation(ATTACKER)
        assert waiting is not None and waiting.attempts == 0
        assert (await confirm(store, ATTACKER, code)).verdict is Verdict.CONFIRMED


class TestHappyPathAndLifetime:
    async def test_the_right_code_from_the_starting_chat_links_it(self, store: LinkStore) -> None:
        code = await _approved(store, VICTIM, key="k1")
        result = await confirm(store, VICTIM, format_code(code))
        assert result.verdict is Verdict.CONFIRMED
        assert result.instance_url == INSTANCE
        account = await store.get(VICTIM)
        assert account is not None
        assert (account.api_key, account.studylife_user_id) == ("k1", "victim-user")

    async def test_a_code_is_single_use(self, store: LinkStore) -> None:
        code = await _approved(store, VICTIM)
        assert (await confirm(store, VICTIM, code)).verdict is Verdict.CONFIRMED
        await store.unlink(VICTIM)
        assert (await confirm(store, VICTIM, code)).verdict is None
        assert await store.get(VICTIM) is None

    async def test_an_expired_code_is_refused_and_discarded(self, store: LinkStore) -> None:
        code = await _approved(store, VICTIM)
        waiting = await store.get_confirmation(VICTIM)
        assert waiting is not None
        late = waiting.created_at + CONFIRMATION_TTL_SECONDS + 1
        assert (await confirm(store, VICTIM, code, now=late)).verdict is Verdict.EXPIRED
        assert await store.get(VICTIM) is None
        assert await store.get_confirmation(VICTIM) is None

    async def test_a_newer_approval_replaces_the_older_code(self, store: LinkStore) -> None:
        old = await _approved(store, VICTIM)
        new = await _approved(store, VICTIM)
        if old != new:
            assert (await confirm(store, VICTIM, old)).verdict is Verdict.WRONG
        assert (await confirm(store, VICTIM, new)).verdict is Verdict.CONFIRMED

    async def test_the_pending_key_is_encrypted_at_rest(self, store: LinkStore) -> None:
        await _approved(store, VICTIM, key="plaintext-secret-key")
        async with store._conn.execute("SELECT encrypted_api_key FROM pending_confirmations") as c:
            row = await c.fetchone()
        assert row is not None and b"plaintext-secret-key" not in row[0]
