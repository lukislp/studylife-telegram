"""Which secret signs which StudyAtrium webhook, and which instance that webhook belongs to.

studylife-webhooks signs every delivery with the secret of the webhook it belongs to and names
that webhook in the X-StudyLife-Webhook-Id header. The bot therefore keeps one entry per webhook
and verifies a delivery with the secret of the webhook it names. An id that is not configured is
rejected: the bot never falls back to another webhook's secret.

The instance comes from the configured entry, never from the delivery, so a delivery cannot claim
to belong to an instance it was not registered for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


class WebhookConfigError(ValueError):
    """STUDYLIFE_WEBHOOKS is not a valid list of webhook entries."""


@dataclass(frozen=True)
class WebhookEndpoint:
    webhook_id: str
    secret: str
    instance_url: str


def parse_endpoints(raw: str, default_instance: str) -> dict[str, WebhookEndpoint]:
    """Parse the JSON list `[{"id": ..., "secret": ..., "instance": ...}]`. `instance` is
    optional and defaults to the bot's default instance. Fails loudly on anything malformed: a
    typo here must stop the start, not silently leave a user's webhook unverified."""
    if not raw.strip():
        return {}
    try:
        entries = json.loads(raw)
    except ValueError as exc:
        raise WebhookConfigError("STUDYLIFE_WEBHOOKS is not valid JSON") from exc
    if not isinstance(entries, list):
        raise WebhookConfigError("STUDYLIFE_WEBHOOKS must be a JSON list")
    endpoints: dict[str, WebhookEndpoint] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise WebhookConfigError("every STUDYLIFE_WEBHOOKS entry must be an object")
        webhook_id = entry.get("id")
        secret = entry.get("secret")
        instance = entry.get("instance", default_instance)
        if not (
            isinstance(webhook_id, str)
            and isinstance(secret, str)
            and isinstance(instance, str)
            and webhook_id.strip()
            and secret.strip()
            and instance.strip()
        ):
            raise WebhookConfigError("every entry needs a non-empty id and secret")
        if webhook_id in endpoints:
            raise WebhookConfigError(f"webhook id {webhook_id!r} is configured twice")
        endpoints[webhook_id] = WebhookEndpoint(webhook_id, secret, instance.strip().rstrip("/"))
    return endpoints
