"""An HTTP transport that routes each URL to a fake service: never the network."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sattleme_checkout import HttpResponse

Handler = Callable[[str], tuple[int, Any]]


@dataclass
class Asked:
    url: str
    headers: dict[str, str]


@dataclass
class FakeHttp:
    """`routes`: URL prefix → handler giving (status, JSON value, or bytes as they are)."""

    routes: dict[str, Handler] = field(default_factory=dict)
    asked: list[Asked] = field(default_factory=list)
    down: set[str] = field(default_factory=set)
    delay: float = 0.0

    def route(self, prefix: str, handler: Handler) -> None:
        self.routes[prefix] = handler

    def urls(self, prefix: str = "") -> list[str]:
        return [asked.url for asked in self.asked if asked.url.startswith(prefix)]

    def agents(self) -> set[tuple[str | None, str | None]]:
        """The `User-Agent` and `Accept` of the requests asked, each pair once."""
        return {(a.headers.get("User-Agent"), a.headers.get("Accept")) for a in self.asked}

    async def __call__(
        self, url: str, *, headers: Mapping[str, str], socket_timeout: float, max_bytes: int
    ) -> HttpResponse:
        self.asked.append(Asked(url, dict(headers)))
        if self.delay:
            await asyncio.sleep(self.delay)
        for prefix, handler in sorted(self.routes.items(), key=lambda item: -len(item[0])):
            if url.startswith(prefix):
                if any(url.startswith(down) for down in self.down):
                    raise ConnectionError(f"{prefix} is down")
                status, value = handler(url)
                body = value if isinstance(value, bytes) else json.dumps(value).encode()
                return HttpResponse(status, body)
        return HttpResponse(404, b"{}")
