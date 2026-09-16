from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment variables / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # From BotFather. Used as the path segment of every outgoing Bot API call
    # (https://api.telegram.org/bot<token>/sendMessage), so it is a credential in the URL -
    # never log a request URL as-is, see telegram_client.redact.
    telegram_bot_token: str

    # Sent by Telegram in X-Telegram-Bot-Api-Secret-Token on every webhook delivery, set once
    # when registering the webhook. This is what makes the public /telegram endpoint safe: a
    # request without it never reaches command handling. Telegram has no request signing, so
    # unlike Discord's Ed25519 this shared secret is the entire authentication story - generate
    # it with real entropy and treat it like a password.
    telegram_webhook_secret: str

    # The only chat this bot answers. Telegram gives a bot's username to anyone who finds it, so
    # without this any stranger could drive the timer of the account below. Comma-separated to
    # allow a second device/chat, but normally one id.
    telegram_allowed_chat_ids: str

    # This single person's StudyLife instance and API key. Like studylife-discord, and unlike
    # studylife-alexa, there is no per-user account linking: the bot only ever acts on one
    # StudyLife account, obtained once via login.py (the same generic OAuth loopback flow
    # studylife-cli uses).
    studylife_base_url: AnyHttpUrl
    studylife_api_key: str

    # HMAC-SHA256 secret StudyLife signs each webhook delivery with, entered when registering
    # this bot's webhook. Only needed for the StudyLife -> Telegram direction (reminders,
    # session events); commands work without it.
    studylife_webhook_secret: str | None = None

    # Public base URL this bot is reachable at, used by the one-off webhook registration helper.
    # Telegram only delivers to HTTPS with a valid certificate.
    public_base_url: AnyHttpUrl | None = None

    @property
    def allowed_chat_ids(self) -> frozenset[int]:
        """Parsed allowlist. Anything unparseable is dropped rather than raising, so one typo in
        the env var cannot take the whole bot down - but an EMPTY result is fatal on purpose
        (see main.py): a bot that answers nobody is a misconfiguration worth failing loudly."""
        ids = set()
        for part in self.telegram_allowed_chat_ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                continue
        return frozenset(ids)
