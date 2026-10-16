"""A fake Subsonic server (what Navidrome speaks) and synthetic songs. Nothing here is real data."""
from __future__ import annotations

from typing import Any, Callable

import httpx

AUTH_KEYS = {"u", "t", "s", "v", "c", "f"}


def song(n: int, **fields: Any) -> dict:
    """A Subsonic ``song`` as Navidrome gives it, with the fields the provider reads."""
    base = {
        "id": f"song-{n}", "title": f"Track {n}", "artist": "Example Artist", "artistId": "artist-1",
        "album": "Example Album", "albumId": "album-1", "coverArt": "mf-album-1", "duration": 200 + n,
        "year": 2020, "track": n, "discNumber": 1, "path": f"Example Artist/Example Album/{n:02d} - Track {n}.mp3",
        "suffix": "mp3", "bitRate": 320, "samplingRate": 44100, "size": 8_000_000 + n, "isrc": [],
        "contentType": "audio/mpeg", "type": "music",
    }
    return {**base, **fields}


def without_auth(params: dict) -> dict:
    """The parameters of a call without the ones that authenticate it."""
    return {key: value for key, value in params.items() if key not in AUTH_KEYS}


class Subsonic:
    """``httpx.get`` of a Subsonic server: answers by endpoint (``search3``, ``getSong``...) with the inner
    ``subsonic-response``, records every call as ``(endpoint, params)``. An endpoint without an answer is ``ok``
    with nothing in it. An answer may be a callable of the params."""

    def __init__(self, answers: dict[str, dict | Callable[[dict], dict]] | None = None):
        self.answers = answers or {}
        self.calls: list[tuple[str, dict]] = []
        self.urls: list[str] = []

    def __call__(self, url, params=None, timeout=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.urls.append(url)
        self.calls.append((endpoint, dict(params or {})))
        answer = self.answers.get(endpoint, {"status": "ok"})
        body = answer(dict(params or {})) if callable(answer) else answer
        return httpx.Response(200, json={"subsonic-response": body}, request=httpx.Request("GET", url))
