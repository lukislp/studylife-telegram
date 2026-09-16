"""One-off browser login to obtain this bot's StudyLife API key.

Run once on a machine with a browser (`python -m studylife_telegram.login https://your-instance`);
the printed key goes into STUDYLIFE_API_KEY, which the deployment reads from a sealed secret. The
bot itself never performs a login - it runs headless in a cluster and only carries the key.

Ported from studylife-cli's login.py, which is the reference implementation of the generic
dynamic-client flow. Keep the wire shapes identical: the server validates them strictly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sys
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

# Fixed, not OS-assigned: the generic flow validates redirect_uri by EXACT match against the
# client's registered AllowedRedirectUris, so a random port could never match. Distinct from
# studylife-cli's 8765-8768 and studylife-vscode's 8775-8778 so all three can be logged in at
# the same time.
CANDIDATE_PORTS = (8785, 8786, 8787, 8788)
CALLBACK_TIMEOUT_SECONDS = 300.0
DEFAULT_CLIENT_ID = "studylife-telegram"


class LoginError(Exception):
    pass


@dataclass
class _Callback:
    assertion: str | None
    state: str | None


def _new_pkce_pair() -> tuple[str, str]:
    """(verifier, challenge): 43 unreserved characters and the unpadded base64url SHA-256 of
    them, exactly the S256 shape StudyLife's connect endpoint validates."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _states_match(received: str | None, expected: str) -> bool:
    return bool(received) and hmac.compare_digest(received or "", expected)


def _make_handler(result: dict[str, _Callback]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            query = parse_qs(urlparse(self.path).query)
            assertions = query.get("assertion")
            states = query.get("state")
            result["value"] = _Callback(
                assertion=assertions[0] if assertions else None,
                state=states[0] if states else None,
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<!doctype html><meta charset=utf-8><title>StudyLife</title>"
                b"<body style='font-family:system-ui;padding:3rem;text-align:center'>"
                b"<h1>Connected</h1><p>You can close this tab.</p>"
            )

        def log_message(self, *args: object) -> None:
            """Silences BaseHTTPRequestHandler's stderr logging - the callback URL carries the
            assertion, and printing it would put a live credential in the terminal."""

    return Handler


def run_login(instance_url: str, client_id: str = DEFAULT_CLIENT_ID) -> str:
    base_url = instance_url.rstrip("/")
    state_token = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _new_pkce_pair()

    result: dict[str, _Callback] = {}
    server: HTTPServer | None = None
    for port in CANDIDATE_PORTS:
        try:
            server = HTTPServer(("127.0.0.1", port), _make_handler(result))
            break
        except OSError:
            continue
    if server is None:
        raise LoginError(f"None of the ports {list(CANDIDATE_PORTS)} are free on 127.0.0.1.")

    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
    query = urlencode(
        {
            "redirect_uri": redirect_uri,
            "state": state_token,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    connect_url = f"{base_url}/connect/client/{client_id}?{query}"
    print(f"Opening your browser to approve the connection:\n  {connect_url}")
    webbrowser.open(connect_url)

    server.timeout = CALLBACK_TIMEOUT_SECONDS
    server.handle_request()
    server.server_close()

    callback = result.get("value")
    if callback is None:
        raise LoginError(
            f"Timed out waiting for the redirect. Either the approval was not completed, or "
            f"'{client_id}' is not registered on that instance yet - see the README."
        )
    if not _states_match(callback.state, state_token):
        raise LoginError("The callback's state did not match. Run this again.")
    if not callback.assertion:
        raise LoginError("The callback carried no assertion - the connection was denied.")

    response = httpx.post(
        f"{base_url}/api/auth/assertion-exchange",
        json={
            "clientId": client_id,
            "assertion": callback.assertion,
            "codeVerifier": code_verifier,
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise LoginError(f"The assertion exchange failed ({response.status_code}).")
    api_key = response.json().get("apiKey")
    if not api_key:
        raise LoginError("The exchange response carried no API key.")
    return str(api_key)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python -m studylife_telegram.login <instance-url>", file=sys.stderr)
        return 2
    try:
        api_key = run_login(sys.argv[1])
    except LoginError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    print("\nSTUDYLIFE_API_KEY:")
    print(api_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
