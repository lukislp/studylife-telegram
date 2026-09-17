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

    # Which chats may use the bot at all. Telegram gives a bot's username to anyone who finds
    # it, so without this any stranger could reach it. Comma-separated ids.
    telegram_allowed_chat_ids: str = ""

    # Drops the allowlist entirely, for a deployment meant to serve strangers: anyone may then
    # /login and drive THEIR OWN account. Deliberately its own flag rather than "an empty
    # allowlist means open" - an allowlist that fails to parse, or an env var that silently goes
    # missing, must never be the thing that opens the bot to the internet.
    telegram_allow_any_chat: bool = False

    # The default StudyLife instance: what /login connects to when no URL is given, and - unless
    # studylife_allow_any_instance is set - the only one permitted.
    studylife_base_url: AnyHttpUrl

    # Further instances users may connect to, comma-separated. studylife_base_url is always
    # included; this only adds to it.
    studylife_extra_instances: str = ""

    # Lets users name ANY https instance. For a public deployment where people bring their own
    # StudyLife. Off by default: without the restriction, "/login https://attacker.example" turns
    # the bot into a request forwarder with the cluster's network position. Check this pod's
    # egress NetworkPolicy before switching it on.
    studylife_allow_any_instance: bool = False

    # Fernet key for the API keys at rest (32 url-safe base64 bytes, e.g.
    # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
    # The database sits on a volume that Velero copies off-site, so the keys are encrypted there
    # rather than lying in a backup as plain text.
    link_encryption_key: str

    # SQLite file holding chat -> account links (store.py). On the persistent volume.
    link_db_path: str = "/app/data/links.db"

    # HMAC-SHA256 secret StudyLife signs each webhook delivery with, entered when registering
    # this bot's webhook. Only needed for the StudyLife -> Telegram direction (reminders,
    # session events); commands work without it.
    studylife_webhook_secret: str | None = None

    # Public base URL this bot is reachable at, used by the one-off webhook registration helper.
    # Telegram only delivers to HTTPS with a valid certificate.
    public_base_url: AnyHttpUrl | None = None

    # The zone StudyLife's naive timestamps are in. StudySessionDto.StartTime carries no offset
    # and means the SERVER's wall clock (studylife-web runs Europe/Berlin); this pod runs UTC
    # unless told otherwise, so the conversion has to be explicit. See times.parse_local.
    studylife_timezone: str = "Europe/Berlin"

    # Minutes before a planned session to send a reminder, mirroring the default of
    # UserSettings.SessionReminderMinutes in StudyLife itself. Empty switches reminders off.
    session_reminder_minutes: str = "60,30,10,5,3,2,1"

    # How often the reminder loop checks the clock, and how often it re-reads the session list.
    # Two intervals because /api/sessions is unwindowed - see reminders.ReminderLoop.
    reminder_tick_seconds: int = 30
    reminder_refresh_seconds: int = 300

    @property
    def allowed_chat_ids(self) -> frozenset[int]:
        """Parsed allowlist. Anything unparseable is dropped rather than raising, so one typo in
        the env var cannot take the whole bot down - but an EMPTY result is fatal on purpose
        (see main.py) unless telegram_allow_any_chat is set: a bot that answers nobody is a
        misconfiguration worth failing loudly, and one that answers everybody must be asked
        for explicitly."""
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

    @property
    def reminder_leads(self) -> tuple[int, ...]:
        """Lead times, largest first, deduplicated. Unparseable or negative entries are dropped
        rather than raising, for the same reason as allowed_chat_ids: one typo should cost the
        reminder it names, not the whole bot."""
        leads = set()
        for part in self.session_reminder_minutes.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                continue
            if value >= 0:
                leads.add(value)
        return tuple(sorted(leads, reverse=True))

    @property
    def allowed_instances(self) -> frozenset[str]:
        """Every instance a chat may connect to. The configured default is always in the set, so
        the common case needs no extra configuration at all."""
        instances = {str(self.studylife_base_url).rstrip("/")}
        for part in self.studylife_extra_instances.split(","):
            part = part.strip().rstrip("/")
            if part:
                instances.add(part)
        return frozenset(instances)
