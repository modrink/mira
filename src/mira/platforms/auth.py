"""Platform authentication abstraction.

GitHub uses an App-installation model that mints short-lived per-installation
tokens; GitLab uses a long-lived group/project access token. Both satisfy
``PlatformAuth`` so handlers can fetch a token and the bot's identity without
knowing which platform they're on.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx


@runtime_checkable
class PlatformAuth(Protocol):
    """How a handler obtains a token and the bot's own identity."""

    async def get_token(self, scope: str | int | None = None) -> str:
        """Return an API token. ``scope`` is the installation id on GitHub,
        unused on token-based platforms."""
        ...

    async def get_bot_identity(self) -> str | None:
        """The bot's handle for self-author detection (GitHub App slug,
        GitLab token user's username). ``None`` if it can't be determined."""
        ...


class GitLabTokenAuth:
    """A static group/project access token. No minting, no expiry handling."""

    def __init__(self, token: str, base_url: str = "https://gitlab.com/api/v4") -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._username_fetched = False
        self._username: str | None = None

    async def get_token(self, scope: str | int | None = None) -> str:
        return self._token

    async def get_bot_identity(self) -> str | None:
        """The token user's username, via ``GET /user`` (cached)."""
        if self._username_fetched:
            return self._username
        self._username_fetched = True
        headers = {"PRIVATE-TOKEN": self._token}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._base_url}/user", headers=headers, timeout=10.0)
                if resp.status_code == 200:
                    username = resp.json().get("username")
                    self._username = username if isinstance(username, str) and username else None
        except (httpx.HTTPError, ValueError):
            self._username = None
        return self._username
