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
| `/courses` | List your courses |
| `/note <text>` | Save a quick note (first line becomes the title) |
| `/help` | The list above |

`/focus Betriebssysteme` matches case-insensitively, exact name first, then unique prefix. An
**ambiguous** prefix is refused rather than guessed: putting study time on the wrong course would
distort the grade and ECTS correlations StudyLife computes from session history.

## Security model

This is a **single-account** bot, like `studylife-discord` and unlike `studylife-alexa`: it holds
one StudyLife API key and acts only on that account. Two things protect it, and both matter:

1. **Telegram's webhook secret token.** Telegram does not sign its deliveries — the only proof a
   request came from Telegram is the secret the bot chose when calling `setWebhook`, echoed back
   in `X-Telegram-Bot-Api-Secret-Token`. That header is the entire door. Generate it with real
   entropy and treat it like a password.
2. **A chat allowlist.** Telegram hands a bot's username to anyone who finds it. Without
   `TELEGRAM_ALLOWED_CHAT_IDS`, any stranger who found the bot could drive your timer. The
   service refuses to start if the list parses to empty.

StudyLife's own webhooks into this bot are authenticated separately, with HMAC-SHA256 over the
raw request body — the same scheme `studylife-webhooks` uses for every other subscriber.

## Setup

### 1. Register the client on your StudyLife instance

Register once through [studylife-developers](https://github.com/lukislp/studylife-developers):

| Field | Value |
| --- | --- |
| Client ID | `studylife-telegram` |
| Redirect URIs | `http://127.0.0.1:8785/callback`, `…8786…`, `…8787…`, `…8788…` |
| Scopes | `TimerState.Get`, `TimerState.Save`, `Courses.GetAll`, `Metrics.GetSummary`, `Notes.Create` |

Four loopback URIs because `redirect_uri` is validated by **exact** match and the login binds
whichever port is free. They differ from `studylife-cli`'s 8765–8768 and `studylife-vscode`'s
8775–8778 so all three can be logged in simultaneously.

### 2. Get the API key

```bash
python -m studylife_telegram.login https://studylife.example.com
```

Opens your browser, you approve, and the key is printed. That value goes into
`STUDYLIFE_API_KEY`. The bot never logs in itself — it runs headless and only carries the key.

### 3. Create the bot

Talk to [@BotFather](https://t.me/BotFather), create a bot, keep the token. Get your own chat id
from [@userinfobot](https://t.me/userinfobot).

### 4. Configure

| Variable | Required | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | From BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | yes | Chosen by you, passed to `setWebhook` |
| `TELEGRAM_ALLOWED_CHAT_IDS` | yes | Comma-separated chat ids that may use the bot |
| `STUDYLIFE_BASE_URL` | yes | Your instance |
| `STUDYLIFE_API_KEY` | yes | From step 2 |
| `STUDYLIFE_WEBHOOK_SECRET` | no | Enables the StudyLife → Telegram direction |
| `PUBLIC_BASE_URL` | no | Used by the webhook registration helper |

### 5. Point Telegram at it

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
