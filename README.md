# StudyLife Telegram Bot

[![CI](https://github.com/lukislp/studylife-telegram/actions/workflows/ci.yml/badge.svg)](https://github.com/lukislp/studylife-telegram/actions/workflows/ci.yml) [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lukislp/studylife-telegram/badge)](https://scorecard.dev/viewer/?uri=github.com/lukislp/studylife-telegram) [![CodeQL](https://github.com/lukislp/studylife-telegram/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/lukislp/studylife-telegram/security/code-scanning)
[![Release](https://img.shields.io/github/v/release/lukislp/studylife-telegram)](https://github.com/lukislp/studylife-telegram/releases)
[![License: AGPL-3.0](https://img.shields.io/github/license/lukislp/studylife-telegram)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12+-3776AB)](https://www.python.org/)

Control your [StudyLife](https://github.com/lukislp/studylife) focus timer from Telegram, and get
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
| `/login [instance]` | Connect your StudyLife account |
| `/logout` | Disconnect this chat |
| `/whoami` | Which account this chat is connected to |
| `/help` | The list above |

`/focus <course>` also remembers the course: when you `/stop`, the run is written to your
history as a session on it. Without a course the timer still runs, but nothing is logged -
the timer row has no course column, so there would be nothing to attribute the time to.

### Accounts

Every chat drives its OWN StudyLife account. `/login` replies with a consent link; approving it
in the browser sends you back to this bot, which stores the resulting API key for your chat. No
file to edit, nothing to install.

Unlike an Alexa skill, which gets an access token on every request and can stay stateless, a
Telegram update carries only a chat id - so the mapping is kept here, in a SQLite file on a small
volume, with the API keys encrypted at rest.

Two things are deliberate:

- **Direct chats only.** A group's chat id belongs to every member, including anyone added later,
  so an account linked there would be drivable by all of them.
- **A leaked login link is not enough to take over a chat.** The PKCE verifier never leaves the
  bot, the state is single-use and expires in ten minutes, and the exchange needs both.

### Reminders

The bot messages you before each planned session, at the same lead times StudyLife itself
uses (`60,30,10,5,3,2,1` minutes by default). This is polled rather than pushed: StudyLife
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
distort the grade and ECTS correlations StudyLife computes from session history.

## Security model

Each Telegram chat links to **its own** StudyLife account: `/login` runs StudyLife's generic
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

StudyLife's own webhooks into this bot are authenticated separately, with HMAC-SHA256 over the
raw request body — the same scheme `studylife-webhooks` uses for every other subscriber.

## Setup

### 1. Register the client on your StudyLife instance

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
| `STUDYLIFE_WEBHOOK_SECRET` | no | Enables the StudyLife → Telegram direction |
| `PUBLIC_BASE_URL` | yes | Where StudyLife returns to after `/login` |
| `LINK_DB_PATH` | no | SQLite file with the chat→account links (default `/app/data/links.db`) |
| `STUDYLIFE_TIMEZONE` | no | Zone StudyLife's timestamps mean (default `Europe/Berlin`) |
| `SESSION_REMINDER_MINUTES` | no | Lead times before a session; empty switches reminders off |
| `REMINDER_TICK_SECONDS` | no | How often the clock is checked (default 30) |
| `REMINDER_REFRESH_SECONDS` | no | How often the session list is re-read (default 300) |

`STUDYLIFE_TIMEZONE` is not cosmetic. StudyLife's timestamps carry no UTC offset and mean
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
| `POST /webhooks/studylife` | StudyLife's outgoing webhooks |
| `GET /connect/callback` | Where StudyLife returns from a `/login` approval |
| `GET /healthz` | Liveness/readiness probe |

Both inbound routes answer `200` for anything they choose to ignore. Telegram retries a non-2xx
and eventually disables the webhook entirely, and StudyLife's delivery service behaves the same —
so "received but not acted on" must never look like a failure.

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
