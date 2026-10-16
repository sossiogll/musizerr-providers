"""LRCLIB: synced and plain lyrics from lrclib.net.

A Musizerr provider that is its own package and its own image (see ``Dockerfile``):
it runs in a sandbox that reaches the network only at the destination it declares.
"""
from __future__ import annotations

import logging
import re

import httpx

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizerr_sdk.interfaces import PROVIDER_API_VERSION, LyricsProvider
from musizerr_sdk.models import LrcLine, Lyrics, Track
from musizerr_sdk.permissions import Permissions
from musizerr_sdk.settings import ProviderSetting

logger = logging.getLogger(__name__)

_BASE = "https://lrclib.net/api"
_LRC_RE = re.compile(r"\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)")


def _parse_lrc(raw: str) -> list[LrcLine]:
    lines: list[LrcLine] = []
    for line in raw.splitlines():
        m = _LRC_RE.match(line)
        if not m:
            continue
        minutes, seconds, frac, text = m.groups()
        # normalise fractional part to milliseconds
        ms_frac = int(frac) * (10 if len(frac) == 2 else 1)
        time_ms = int(minutes) * 60_000 + int(seconds) * 1000 + ms_frac
        lines.append(LrcLine(time_ms=time_ms, text=text))
    return lines


def _to_lyrics(data: dict) -> Lyrics | None:
    plain = data.get("plainLyrics") or None
    synced_raw = data.get("syncedLyrics") or None
    if not plain and not synced_raw:
        return None
    return Lyrics(
        plain=plain,
        synced=_parse_lrc(synced_raw) if synced_raw else [],
        source="lrclib",
    )


class LrclibLyricsProvider(LyricsProvider):
    name = "lrclib"
    label = "LRCLIB"
    provider_api_version = PROVIDER_API_VERSION
    # The only place it talks to; the sandbox grants nothing else.
    permissions = Permissions(egress=("lrclib.net:443",))
    # Public service: no account, whoever the viewer.
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def config_schema(self) -> list[ProviderSetting]:
        return []

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session: AuthSession) -> str | None:
        try:
            resp = httpx.get(f"{_BASE}/search", params={"q": "test"}, timeout=self.context.http_timeout)
            resp.raise_for_status()
            return None
        except httpx.HTTPError as e:
            return str(e)

    def get_lyrics(self, track: Track, session: AuthSession) -> Lyrics | None:
        # 1) exact match
        lyrics = self._get_exact(track)
        if lyrics:
            return lyrics
        # 2) fallback: search
        return self._search_fallback(track)

    def _get_exact(self, track: Track) -> Lyrics | None:
        params: dict[str, str | int] = {
            "artist_name": track.artist,
            "track_name": track.title,
            "album_name": track.album,
        }
        if track.duration_ms:
            params["duration"] = track.duration_ms // 1000
        try:
            resp = httpx.get(f"{_BASE}/get", params=params, timeout=self.context.http_timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return _to_lyrics(resp.json())
        except httpx.HTTPError:
            logger.debug("LRCLIB exact match failed for %s - %s", track.artist, track.title)
            return None

    def _search_fallback(self, track: Track) -> Lyrics | None:
        query = f"{track.artist} {track.title}"
        try:
            resp = httpx.get(f"{_BASE}/search", params={"q": query}, timeout=self.context.http_timeout)
            resp.raise_for_status()
            results = resp.json()
            if not results:
                return None
            return _to_lyrics(results[0])
        except httpx.HTTPError:
            logger.debug("LRCLIB search failed for %s", query)
            return None
