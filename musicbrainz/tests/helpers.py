"""Small helpers of these tests: a track to build, a fake HTTP, and a call made through the runner."""
from __future__ import annotations

from typing import Any

import httpx

from musizerr_sdk import protocol
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.identifiers import Identifier
from musizerr_sdk.models import Track
from musizerr_sdk.runner import ProviderRunner


def make_track(
    title: str = "Song",
    artist: str = "Artist",
    album: str = "Album",
    *,
    duration_ms: int = 200_000,
    provider: str = "other",
    provider_id: str | None = None,
    isrc: str | None = None,
    mbid: str | None = None,
    year: int | None = None,
) -> Track:
    ids: list[Identifier] = []
    if isrc:
        ids.append(Identifier(type="isrc", value=isrc))
    if mbid:
        ids.append(Identifier(type="mbid", value=mbid))
    return Track(
        title=title, artist=artist, album=album, duration_ms=duration_ms, provider=provider,
        provider_id=provider_id or f"{provider}-{title}", external_ids=ids, year=year,
    )


class Http:
    """A fake ``httpx.get``: answers by the end of the URL's path, records the calls (url, params)."""

    def __init__(self, routes: dict[str, dict], status: int = 200):
        self.routes = routes
        self.status = status
        self.calls: list[tuple[str, dict]] = []
        self.headers: list[dict] = []

    def __call__(self, url, params=None, timeout=None, headers=None, **kwargs):
        self.calls.append((url, dict(params or {})))
        self.headers.append(dict(headers or {}))
        for suffix, body in self.routes.items():
            if url.endswith(suffix):
                return httpx.Response(self.status, json=body, request=httpx.Request("GET", url))
        return httpx.Response(404, json={}, request=httpx.Request("GET", url))


class RemoteFailure(Exception):
    """A failure the runner classified (not one of the contract's own exceptions)."""

    def __init__(self, remote) -> None:
        super().__init__(remote.message)
        self.kind = remote.kind
        self.type_name = remote.type_name
        self.retry_after = remote.retry_after


def via_runner(factory, method: str, *args: Any, settings: dict | None = None, provider_name: str = "provider", **kwargs: Any):
    """``method`` called the way the host calls it in a sandbox: encoded, handled by a runner, decoded."""
    context = StaticProviderContext(settings=settings, provider_name=provider_name)
    request = protocol.call_request(method, args, kwargs, context.snapshot())
    return protocol.read_reply(ProviderRunner(factory).handle(request), RemoteFailure)
