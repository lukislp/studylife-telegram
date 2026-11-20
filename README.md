# StudyAtrium Telegram Bot

(formerly StudyLife)

[![CI](https://github.com/lukislp/studylife-telegram/actions/workflows/ci.yml/badge.svg)](https://github.com/lukislp/studylife-telegram/actions/workflows/ci.yml) [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lukislp/studylife-telegram/badge)](https://scorecard.dev/viewer/?uri=github.com/lukislp/studylife-telegram) [![CodeQL](https://github.com/lukislp/studylife-telegram/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/lukislp/studylife-telegram/security/code-scanning)
[![Release](https://img.shields.io/github/v/release/lukislp/studylife-telegram)](https://github.com/lukislp/studylife-telegram/releases)
[![License: AGPL-3.0](https://img.shields.io/github/license/lukislp/studylife-telegram)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12+-3776AB)](https://www.python.org/)

Control your [StudyAtrium](https://github.com/lukislp/studylife) focus timer from Telegram, and get
session events and goal reminders in the chat you already have open.

The timer is shared across every device, so a session started here shows up in the web app, the
tray app, VS Code and Home Assistant alike — and the other way round.

## Commands

| Command | What it does |
| --- | --- |
| `/status` | Is a focus session running? |
| `/focus [course]` | Start a session, optionally for a named course |
| `/pause` | Pause the running session |
| `/stop` | Stop it |
| `/today` | Hours today and this week, plus the current streak |
| `/next` | Next course goal and its countdown |
| `/agenda` | Your next planned study sessions |
| `/courses` | List your courses |
| `/note <text>` | Save a quick note (first line becomes the title) |
| `/login [instance]` | Connect your StudyAtrium account |
| `/logout` | Disconnect this chat |
| `/whoami` | Which account this chat is connected to |
| `/help` | The list above |

`/focus <course>` also remembers the course: when you `/stop`, the run is written to your
history as a session on it. Without a course the timer still runs, but nothing is logged -
the timer row has no course column, so there would be nothing to attribute the time to.

### Accounts

Every chat drives its OWN StudyAtrium account. `/login` replies with a consent link; approving it
in the browser sends you back to this bot, whose page then shows an 8-digit code. Type it into the
chat (`/confirm 1234 5678`, or just paste the digits) and only then is the account connected.
No file to edit, nothing to install.

Unlike an Alexa skill, which gets an access token on every request and can stay stateless, a
Telegram update carries only a chat id - so the mapping is kept here, in a SQLite file on a small
volume, with the API keys encrypted at rest.

Two things are deliberate:

- **Direct chats only.** A group's chat id belongs to every member, including anyone added later,
  so an account linked there would be drivable by all of them.
- **A leaked login link is not enough to take over a chat.** The PKCE verifier never leaves the
  bot, the state is single-use and expires in ten minutes, and the exchange needs both.
- **A forwarded login link cannot attach someone else's account to your chat.** Approving the
  link does not connect anything by itself: the confirmation code appears only in the browser
  that approved, and must be typed into the chat that started `/login`. Someone who is sent a
  forwarded link sees the code on their own screen, where it is useless to the sender. The code
  is random (CSPRNG), single-use, expires after ten minutes, is compared in constant time, and
  five wrong tries discard the pending link (`confirmation.py`, `link_service.py`).

### Reminders

The bot messages you before each planned session, at the same lead times StudyAtrium itself
uses (`60,30,10,5,3,2,1` minutes by default). This is polled rather than pushed: StudyAtrium
publishes no "a session is about to start" event - its webhook catalogue is reactive
(`session.created` fires when you PLAN a session, not when it comes due), so the bot watches
the clock itself, exactly as the web app's own client does.

Worth knowing:

- The session list is re-read every `REMINDER_REFRESH_SECONDS`, so a session created only a
  few minutes before it starts may miss its earliest lead time.
- Only the nearest lead is ever sent. After a restart shortly before a session you get one
  message, not the whole ladder of them.
- Reminders go to the same allowlist that may drive the account - never to a separately
  configured target.

`/focus Betriebssysteme` matches case-insensitively, exact name first, then unique prefix. An
**ambiguous** prefix is refused rather than guessed: putting study time on the wrong course would
distort the grade and ECTS correlations StudyAtrium computes from session history.

## Security model

Each Telegram chat links to **its own** StudyAtrium account: `/login` runs StudyAtrium's generic
dynamic-client consent flow (PKCE, callback on this bot's `PUBLIC_BASE_URL`) and stores the
resulting per-chat API key encrypted at rest (`LINK_ENCRYPTION_KEY`, SQLite at `LINK_DB_PATH`).
The bot never holds an account-wide key, and a chat can only ever reach the account it linked -
the single-use `/login` state is what binds a callback to a chat. Three things protect it:

1. **Telegram's webhook secret token.** Telegram does not sign its deliveries — the only proof a
   request came from Telegram is the secret the bot chose when calling `setWebhook`, echoed back
   in `X-Telegram-Bot-Api-Secret-Token`. That header is the entire door. Generate it with real
   entropy and treat it like a password.
2. **A chat allowlist, or an explicit opt-out.** Telegram hands a bot's username to anyone who
   finds it. `TELEGRAM_ALLOWED_CHAT_IDS` limits who may talk to the bot at all; the service
   refuses to start if that list parses to empty. Set `TELEGRAM_ALLOW_ANY_CHAT=true` only when
   you mean it: then anyone may `/login` and drive their own account (never somebody else's).
3. **Instance pinning.** `/login` connects to `STUDYLIFE_BASE_URL` and the instances listed in
   `STUDYLIFE_EXTRA_INSTANCES`, nothing else, unless `STUDYLIFE_ALLOW_ANY_INSTANCE` is set.

StudyAtrium's own webhooks into this bot are authenticated separately, with HMAC-SHA256 over the
raw request body — the same scheme `studylife-webhooks` uses for every other subscriber.

## Setup

### 1. Register the client on your StudyAtrium instance

Register once through [studylife-developers](https://github.com/lukislp/studylife-developers):

| Field | Value |
| --- | --- |
| Client ID | `studylife-telegram` |
| Redirect URIs | `https://<your PUBLIC_BASE_URL>/connect/callback` |
| Scopes | `TimerState.Get`, `TimerState.Save`, `Courses.GetAll`, `Metrics.GetSummary`, `Notes.Create`, `Sessions.GetAll`, `Sessions.GetHistory`, `Sessions.Create` |

The three `Sessions` scopes are what make `/today`, `/agenda` and the reminders possible:
the metrics API has no daily figure and no upcoming-session list, so both are derived from
the session data. `Sessions.Create` writes the session a `/stop` produces.

One URI, and only one is needed: the bot always redirects to its own callback. `redirect_uri` is
validated by **exact** match, so spell it exactly as `PUBLIC_BASE_URL` + `/connect/callback` -
a trailing slash or a different host is refused, which is the point.

Earlier versions also registered loopback URIs for a local `login.py` helper that produced one
account-wide API key. Both are gone: keys are per chat now, and `/login` obtains them from
inside Telegram.

### 2. Create the bot

Talk to [@BotFather](https://t.me/BotFather), create a bot, keep the token. Get your own chat id
from [@userinfobot](https://t.me/userinfobot).

### 3. Configure

`*` `TELEGRAM_ALLOWED_CHAT_IDS` is required unless `TELEGRAM_ALLOW_ANY_CHAT` is `true`. The
service refuses to start with neither, on purpose: an env var that silently goes missing must
never be the thing that opens the bot to everyone. The same reasoning applies to
`STUDYLIFE_ALLOW_ANY_INSTANCE` - without it, `/login https://somewhere-else` would make the bot
issue requests to whatever host a stranger names, from inside the cluster network.

Generate the encryption key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Rotating it breaks no data, but every chat has to `/login` again.


| Variable | Required | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | From BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | yes | Chosen by you, passed to `setWebhook` |
| `TELEGRAM_ALLOWED_CHAT_IDS` | yes* | Comma-separated chat ids that may use the bot |
| `TELEGRAM_ALLOW_ANY_CHAT` | no | `true` drops the allowlist: anyone may connect their own account |
| `STUDYLIFE_BASE_URL` | yes | The default instance `/login` connects to |
| `STUDYLIFE_EXTRA_INSTANCES` | no | Further instances users may connect to |
| `STUDYLIFE_ALLOW_ANY_INSTANCE` | no | `true` allows any https instance |
| `LINK_ENCRYPTION_KEY` | yes | Fernet key encrypting the stored API keys |
| `STUDYLIFE_WEBHOOK_SECRET` | no | Enables the StudyAtrium → Telegram direction (per-user, see below) |
| `PUBLIC_BASE_URL` | yes | Where StudyAtrium returns to after `/login` |
| `STUDYLIFE_PURGE_SECRET` | no | Enables account-deletion purges (see below); unset = route answers 503 |
| `LINK_DB_PATH` | no | SQLite file with the chat→account links (default `/app/data/links.db`) |
| `STUDYLIFE_TIMEZONE` | no | Zone StudyAtrium's timestamps mean (default `Europe/Berlin`) |
| `SESSION_REMINDER_MINUTES` | no | Lead times before a session; empty switches reminders off |
| `REMINDER_TICK_SECONDS` | no | How often the clock is checked (default 30) |
| `REMINDER_REFRESH_SECONDS` | no | How often the session list is re-read (default 300) |

`STUDYLIFE_TIMEZONE` is not cosmetic. StudyAtrium's timestamps carry no UTC offset and mean
the server's local wall clock; this container would otherwise run UTC and every reminder
would fire an hour or two off.

### 4. Point Telegram at it

Telegram only delivers to HTTPS with a valid certificate:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=$PUBLIC_BASE_URL/telegram" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET" \
  -d "allowed_updates=[\"message\"]"
```

## Endpoints

| Route | Purpose |
| --- | --- |
| `POST /telegram` | Updates from Telegram |
| `POST /webhooks/studylife` | StudyAtrium's outgoing webhooks |
| `GET /connect/callback` | Where StudyAtrium returns from a `/login` approval |
| `POST /internal/users/{id}/purge` | Account deletion (cluster-internal, shared secret) |
| `GET /healthz` | Liveness/readiness probe |

Both inbound routes answer `200` for anything they choose to ignore. Telegram retries a non-2xx
and eventually disables the webhook entirely, and StudyAtrium's delivery service behaves the same —
so "received but not acted on" must never look like a failure.

## Webhook deliveries (StudyAtrium to Telegram)

`POST /webhooks/studylife` is off (404) unless `STUDYLIFE_WEBHOOK_SECRET` is set. An event goes to
the chats linked to the user it belongs to, and to nobody else. The body is the JSON
`studylife-webhooks` delivers, signed with HMAC-SHA256 over the raw bytes
(`X-StudyLife-Webhook-Signature`, compared in constant time), and it must name its owner:

```json
{
  "event_type": "session.completed",
  "occurred_at": "2026-11-19T12:00:00Z",
  "user_id": 7,
  "payload": { "courseName": "Analysis II" }
}
```

- `user_id` is required and covered by the signature. It is a positive integer or a non-empty
  string, matched against the user id stored when the chat was linked. A body without a usable
  `user_id` is rejected with `400` and nothing is sent. There is no broadcast to all chats.
- A wrong or missing signature is `403`. A user with no linked chat, or an event type the bot does
  not announce, is a plain `200` with no delivery, so the sender does not retry.
- User ids are only unique per StudyAtrium instance. Point `STUDYLIFE_WEBHOOK_SECRET` at one
  instance's webhook service; if the bot is linked to several instances, two users with the same
  id on different instances would both be notified.

## Account deletion (purge)

When a user deletes their StudyAtrium account, the server calls this bot so nothing is left
behind (GDPR Art. 17). See [PRIVACY.md](PRIVACY.md) for what the bot stores.

```
POST /internal/users/{user_id}/purge
X-StudyLife-Shared-Secret: <STUDYLIFE_PURGE_SECRET>
{"key_hashes": ["<sha256 hex of an API key issued to this bot>", ...]}
```

- `key_hashes` are the lowercase hex SHA-256 of every API key the server issued to this bot for
  that user (at most 50, each exactly 64 hex characters; otherwise `422`). The bot hashes each
  stored key and deletes the matching links, any approval still waiting for its code, and the
  open `/login` of those chats, in one transaction.
- Rows are also deleted by user id, but only for the default instance (`STUDYLIFE_BASE_URL`),
  never by id alone: user ids are only unique per StudyAtrium instance and this bot may serve
  several. This is what removes a link whose key was revoked before the deletion (its hash is
  then no longer known to the server) or whose key can no longer be decrypted.
- `204` on success, also when nothing matched or on a repeat call. `401` for a wrong or missing
  secret, `503` when `STUDYLIFE_PURGE_SECRET` is unset (the route is never open without one).
- The chat is not messaged: the account is already gone. The log gets the user id and counts only.
- The route is cluster-internal. It is not on the public funnel (`k8s/04-tailscale-funnel.yaml`),
  and `k8s/03-network-policies.yaml` lets only the StudyAtrium server pods reach the port. On the
  server, configure `AddonPurge:Telegram:BaseUrl` and `AddonPurge:Telegram:SharedSecret` (the
  same value as `STUDYLIFE_PURGE_SECRET`).

### TLS

The purge call carries the shared secret and the SHA-256 hashes of the user's API keys, so it should not cross the network in plain text. With both files set, [`tls.py`](src/studylife_telegram/tls.py) starts a second `uvicorn.Server` in the same process that serves the same app over HTTPS on `STUDYLIFE_TELEGRAM_TLS_PORT` (default `8443`), TLS 1.2 minimum. Only one file set, an unloadable certificate or a port that cannot be bound stops the start, so the service never runs without the TLS listener it was asked for. uvicorn does not reload certificates itself; `tls.py` checks the files periodically and loads a renewed pair into the running listener, so new handshakes use it without a restart. Port `8000` stays plain HTTP for the kubelet probes.

| Variable | Default | Meaning |
| --- | --- | --- |
| `STUDYLIFE_TELEGRAM_TLS_CERT_FILE` | *(unset)* | PEM certificate chain. Set together with the key file, or not at all. |
| `STUDYLIFE_TELEGRAM_TLS_KEY_FILE` | *(unset)* | PEM private key. |
| `STUDYLIFE_TELEGRAM_TLS_PORT` | `8443` | Port of the HTTPS listener. |
| `STUDYLIFE_TELEGRAM_TLS_RELOAD_INTERVAL_SECONDS` | `300` | How often the files are checked for a renewed certificate. |

Unset (the default), the add-on only serves plain HTTP, which a single-host Docker Compose setup on a private network keeps working with: StudyAtrium then reaches it over `http://` and logs a warning at startup. To encrypt the hop:

1. Issue a certificate whose subject alternative names include the host name StudyAtrium uses in `AddonPurge__Telegram__BaseUrl` (for example the Compose service name `studylife-telegram`), signed by a CA of your own. In k3s this is the cert-manager `Certificate` in [`k8s/05-tls-certificate.yaml`](k8s/05-tls-certificate.yaml), issued by StudyAtrium's internal CA.
2. Mount the certificate and key into the container and set the two `STUDYLIFE_TELEGRAM_TLS_*_FILE` variables.
3. Give StudyAtrium the CA certificate (`AddonTls__CaCertificatePath`) and switch the URL to `AddonPurge__Telegram__BaseUrl=https://studylife-telegram:8443`. StudyAtrium validates the chain and the host name; there is no setting that turns the validation off. Once every add-on serves TLS, `AddonTls__RequireHttps=true` makes StudyAtrium refuse any `http://` add-on URL.

Rollout in k3s, in this order: (1) merge and release this repository (Certificate, port and TLS variables through Flux; the listener starts with the release image); (2) apply the updated `k8s/03-network-policies.yaml` by hand (it only adds `8443` and is not Flux-managed); (3) StudyAtrium's switch of `AddonPurge__Telegram__BaseUrl` to `https://studylife-telegram.studylife-telegram.svc.cluster.local:8443` and the removal of `AddonPurge__Telegram__AllowPlainHttp` (StudyAtrium repository). Purges that fail in between stay in StudyAtrium's outbox and are retried, never dropped.
## Revoked keys and stale logins

- When StudyAtrium answers `401` to a chat's stored API key (revoked, or the account is gone), the
  bot removes that chat's link and tells the chat to `/login` again. This happens both when the
  chat sends a command and when the reminder loop refreshes its sessions. Other errors (403, 5xx)
  keep the link.
- Open `/login` requests and approvals still waiting for their code are deleted once they are
  older than their ten-minute lifetime. The sweep runs at startup and whenever a new login or
  approval is stored.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

The modules worth testing are the ones without I/O: `verify.py` (both authentication paths),
`commands.py` (parsing, formatting, course matching). Those are covered directly; the FastAPI
layer stays thin enough to read.

## Licence

AGPL-3.0-or-later — see [LICENSE](LICENSE).
