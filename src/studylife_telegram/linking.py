"""Connecting one Telegram chat to one StudyLife account.

Same wire shape as login.py's one-off browser flow, with the loopback redirect replaced by this
bot's own public callback - which is what makes it usable from a phone, with nothing installed.
StudyLife needs no change for it: ConsentRedirectPolicy routes dynamically registered clients
past the hardcoded per-audience allow-list and matches them against their own registered
AllowedRedirectUris instead, and any absolute https URL may be registered there.

What protects the flow, given that the callback is on the public internet:

- The PKCE verifier never leaves this process. /api/auth/assertion-exchange is anonymous and the
  assertion is its only credential, but the exchange also requires the verifier - so intercepting
  the browser redirect alone redeems nothing.
- The state is single-use, expires in ten minutes and is the ONLY thing binding a callback to a
  chat. Without that binding, anyone who reached the callback could attach their own account to
  somebody else's chat, or attach an account they control to a chat they do not.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx

# Generous on purpose: the exchange happens once per login and a slow instance should not
# turn into "that approval is no longer valid", which is what the user would read next.
EXCHANGE_TIMEOUT_SECONDS = 30.0


class LinkError(Exception):
    """Something the user can act on - the text goes straight into the chat."""


def new_pkce_pair() -> tuple[str, str]:
    """(verifier, challenge): 43 unreserved characters and the unpadded base64url SHA-256 of
    them, exactly the S256 shape StudyLife's connect endpoint validates."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_state() -> str:
    return secrets.token_urlsafe(32)


def normalise_instance(url: str) -> str:
    return url.strip().rstrip("/")


def instance_allowed(url: str, allowed: frozenset[str], allow_any: bool) -> bool:
    """Whether the bot may talk to this StudyLife instance.

    Not a formality. Without it, `/login https://attacker.example` would make the bot fetch and
    post to whatever host a stranger names - an open request forwarder sitting inside the
    cluster network. `allow_any` exists for a future public deployment where users bring their
    own instances; it is off by default, and turning it on should come with a look at what this
    pod's NetworkPolicy permits egress to.
    """
    candidate = normalise_instance(url)
    if not candidate.lower().startswith("https://"):
        # http would put the API key on the wire in clear text; loopback is meaningless from
        # inside a pod.
        return False
    if allow_any:
        return True
    return candidate in {normalise_instance(entry) for entry in allowed}


def build_connect_url(
    instance_url: str, client_id: str, redirect_uri: str, state: str, challenge: str
) -> str:
    query = urlencode(
        {
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{normalise_instance(instance_url)}/connect/client/{client_id}?{query}"


def callback_url(public_base_url: str) -> str:
    """The one redirect URI that has to be registered on the OAuth client, exactly as spelled
    here - the server matches it character for character."""
    return f"{normalise_instance(public_base_url)}/connect/callback"


async def exchange_assertion(
    instance_url: str, client_id: str, assertion: str, code_verifier: str
) -> tuple[str, str | None]:
    """Redeems the single-use assertion for a freshly rotated API key.

    Returns (api_key, studylife_user_id). Every failure is a LinkError with wording the user can
    act on: a 401 here almost always means the assertion was already used or has expired, which
    is a "try /login again" situation rather than a bug.
    """
    async with httpx.AsyncClient(timeout=EXCHANGE_TIMEOUT_SECONDS) as http:
        try:
            response = await http.post(
                f"{normalise_instance(instance_url)}/api/auth/assertion-exchange",
                json={
                    "clientId": client_id,
                    "assertion": assertion,
                    "codeVerifier": code_verifier,
                },
            )
        except httpx.HTTPError as exc:
            raise LinkError("Could not reach StudyLife to finish connecting.") from exc
    if response.status_code == 401:
        raise LinkError("That approval is no longer valid. Send /login and try again.")
    if response.status_code >= 400:
        raise LinkError(f"StudyLife refused the connection ({response.status_code}).")
    payload = response.json()
    api_key = payload.get("apiKey")
    if not api_key:
        raise LinkError("StudyLife returned no API key.")
    user_id = payload.get("userId")
    return str(api_key), None if user_id is None else str(user_id)


def is_private_chat(chat: object) -> bool:
    """Only one-to-one chats may connect an account.

    A group's chat id belongs to every member, including anyone added later, so an account
    linked there would be drivable by all of them. Telegram offers no per-member scoping of a
    chat, and binding to the sender instead would put one person's study times and course names
    in front of the whole group on every reply.
    """
    return isinstance(chat, dict) and chat.get("type") == "private"
