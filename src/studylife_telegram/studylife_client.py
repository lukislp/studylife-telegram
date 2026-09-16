"""Typed async client for exactly the endpoints this bot is scoped for.

Every method corresponds to one scope the bot requests at registration, and nothing here reaches
an endpoint outside that set - a call added without the matching scope authenticates fine and
then fails with 403 on every request, which is a confusing way to find out.
"""

from __future__ import annotations

from typing import Any

import httpx


class StudyLifeApiError(Exception):
    """Any non-2xx response, carrying the status so callers can tell a permission problem
    (403, never self-healing) from a transient one."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"StudyLife API returned {status_code}: {body.strip()[:200]}")
        self.status_code = status_code
        self.body = body


class StudyLifeClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Api-Key": api_key},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> StudyLifeClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise StudyLifeApiError(response.status_code, response.text)
        return response

    async def get_timer_state(self) -> dict[str, Any]:
        return dict((await self._request("GET", "/api/timerstate")).json())

    async def save_timer_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Writes the timer. The server is last-write-wins and answers 200 with the
        authoritative row rather than 409 (TimerStateService.SaveAsync), so what comes back -
        not what was sent - is what should be reported to the user."""
        return dict((await self._request("PUT", "/api/timerstate", json=state)).json())

    async def list_courses(self) -> list[dict[str, Any]]:
        return list((await self._request("GET", "/api/courses")).json())

    async def get_metrics_summary(self) -> dict[str, Any]:
        """Every study figure this bot reports comes from here - the one place StudyLife
        calculates them (see MetricsController). Recomputing any of them locally would
        eventually disagree with the web app over some edge case nobody thought to check."""
        return dict((await self._request("GET", "/api/metrics/summary")).json())

    async def create_note(self, title: str, content: str) -> dict[str, Any]:
        response = await self._request(
            "POST", "/api/notes", json={"title": title, "content": content}
        )
        return dict(response.json())
