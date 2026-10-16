"""MusicBrainz: the open music encyclopedia, as a metadata provider (search, lookups, discography, ISRC)."""
from __future__ import annotations
from typing import Any

import httpx

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizerr_sdk.identifiers import ISRC, MBID, PROVIDER_ID, Identifier
from musizerr_sdk.interfaces import PROVIDER_API_VERSION, MetadataProvider
from musizerr_sdk.models import Album, Artist, ArtistRef, Track
from musizerr_sdk.permissions import Permissions
from musizerr_sdk.settings import ProviderSetting
from musizerr_sdk.text import parse_year

_BASE = "https://musicbrainz.org/ws/2"
_RELEASE_GROUPS_PAGE = 100  # GET /release-group
_HEADERS = {"User-Agent": "Musizerr/1.0 (https://github.com/musizerr/musizerr)"}


class MusicBrainzMetadataProvider(MetadataProvider):
    name = "musicbrainz"
    label = "MusicBrainz"
    provider_api_version = PROVIDER_API_VERSION
    # The one place it talks to: every request goes to _BASE, and none follows a redirect.
    permissions = Permissions(egress=("musicbrainz.org:443",))
    # Public service: no account, whoever the viewer.
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def test_connection(self, session: AuthSession) -> str | None:
        try:
            resp = httpx.get(
                f"{_BASE}/artist",
                params={"query": "test", "limit": 1, "fmt": "json"},
                headers=_HEADERS,
                timeout=self.context.http_timeout,
            )
            resp.raise_for_status()
            return None
        except httpx.HTTPError as e:
            return str(e)

    def config_schema(self) -> list[ProviderSetting]:
        return [
            ProviderSetting(
                key="search_limit", type="int", default=None,
                label="Search result limit",
                required=False,
                description="Max results per request (MusicBrainz supports up to 100).",
            ),
        ]

    def validate_config(self, config: dict) -> bool:
        return True

    def search_tracks(self, query: str, session: AuthSession, limit: int = 20) -> list[Track]:
        params = {
            "query": query,
            "limit": limit,
            "fmt": "json",
        }
        resp = httpx.get(f"{_BASE}/recording", params=params, headers=_HEADERS, timeout=self.context.http_timeout)
        resp.raise_for_status()
        data = resp.json()

        tracks = []
        for rec in data.get("recordings", []):
            tracks.extend(self._recording_to_tracks(rec))
        return tracks

    def search_albums(self, query: str, session: AuthSession, limit: int = 20) -> list[Album]:
        params = {"query": query, "limit": limit, "fmt": "json"}
        resp = httpx.get(f"{_BASE}/release", params=params, headers=_HEADERS, timeout=self.context.http_timeout)
        resp.raise_for_status()
        data = resp.json()

        albums = []
        for release in data.get("releases", []):
            album = self._release_to_album(release)
            if album:
                albums.append(album)
        return albums

    def search_artists(self, query: str, session: AuthSession, limit: int = 20) -> list[Artist]:
        params = {"query": query, "limit": limit, "fmt": "json"}
        resp = httpx.get(f"{_BASE}/artist", params=params, headers=_HEADERS, timeout=self.context.http_timeout)
        resp.raise_for_status()
        data = resp.json()

        artists = []
        for a in data.get("artists", []):
            artist = Artist(name=a.get("name", ""), provider=self.name, provider_id=a["id"])
            artist.add_id(Identifier(type=MBID, value=a["id"]))
            artists.append(artist)
        return artists

    def supports_isrc_lookup(self) -> bool:
        return True

    def find_by_isrc(self, isrc: str, session: AuthSession) -> Track | None:
        # inc=recordings non è valido per l'endpoint /isrc/ (restituisce già recordings di default)
        resp = httpx.get(
            f"{_BASE}/isrc/{isrc}",
            params={"fmt": "json", "inc": "artist-credits+releases"},
            headers=_HEADERS,
            timeout=self.context.http_timeout,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        recordings = resp.json().get("recordings", [])
        if not recordings:
            return None
        tracks = self._recording_to_tracks(recordings[0])
        return tracks[0] if tracks else None

    def find_related_tracks(self, track: Track, session: AuthSession, limit: int = 20) -> list[Track]:
        """Le altre uscite della stessa registrazione. Una traccia di un altro
        provider si cerca col proprio id fra i suoi identificatori, o col suo
        MBID (quello di una registrazione): mai col ``provider_id`` altrui."""
        own_id = self._own_track_id(track)
        recording_id = self._parse_track_provider_id(own_id)[0] if own_id else None
        if not recording_id:
            mbid = track.get_id(MBID)
            recording_id = mbid.value if mbid else None
        if not recording_id:
            return []
        params = {"inc": "isrcs+artist-credits+releases", "fmt": "json"}
        resp = httpx.get(f"{_BASE}/recording/{recording_id}", params=params, headers=_HEADERS, timeout=self.context.http_timeout)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        related = self._recording_to_tracks(resp.json())
        return [candidate for candidate in related if candidate.provider_id != own_id][:limit]

    def _own_track_id(self, track: Track) -> str | None:
        """L'id MusicBrainz della traccia (``recording::release``), se ne ha uno."""
        if track.provider == self.name:
            return track.provider_id
        identifier = track.get_id(PROVIDER_ID, self.name)
        return identifier.value if identifier else None

    def get_track(self, provider_id: str, session: AuthSession) -> Track | None:
        recording_id, release_id = self._parse_track_provider_id(provider_id)
        params = {"inc": "isrcs+artist-credits+releases", "fmt": "json"}
        resp = httpx.get(f"{_BASE}/recording/{recording_id}", params=params, headers=_HEADERS, timeout=self.context.http_timeout)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        tracks = self._recording_to_tracks(resp.json())
        if release_id:
            return next((track for track in tracks if track.album_provider_id == release_id), None)
        return tracks[0] if len(tracks) == 1 else None

    def get_artist(self, provider_id: str, session: AuthSession) -> Artist | None:
        params = {"fmt": "json"}
        resp = httpx.get(f"{_BASE}/artist/{provider_id}", params=params, headers=_HEADERS, timeout=self.context.http_timeout)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        artist = Artist(name=data.get("name", ""), provider=self.name, provider_id=data["id"])
        artist.add_id(Identifier(type=MBID, value=data["id"]))
        return artist

    def get_artist_albums(self, provider_id: str, session: AuthSession, limit: int = 50) -> list[Album]:
        albums: list[Album] = []
        offset = 0
        while offset < limit:
            page_size = min(limit - offset, _RELEASE_GROUPS_PAGE)
            params = {
                "artist": provider_id,
                "type": "album|single|ep",
                "limit": page_size,
                "offset": offset,
                "fmt": "json",
            }
            resp = httpx.get(f"{_BASE}/release-group", params=params, headers=_HEADERS, timeout=self.context.http_timeout)
            resp.raise_for_status()
            data = resp.json()
            groups = data.get("release-groups", [])
            albums.extend(album for album in (self._release_group_to_album(rg) for rg in groups) if album)
            offset += len(groups)
            if not groups or offset >= data.get("release-group-count", 0):
                break
        return albums

    def _release_group_to_album(self, rg: dict[str, Any]) -> Album | None:
        title = rg.get("title")
        if not title:
            return None
        artist_credits = rg.get("artist-credit", [])
        album = Album(
            title=title,
            artist=self._artist_name(artist_credits),
            year=parse_year(rg.get("first-release-date", "")),
            track_count=rg.get("track-count"),
            provider=self.name,
            provider_id=rg["id"],
            album_type=_release_group_type(rg),
        )
        album.add_id(Identifier(type=MBID, value=rg["id"]))
        return album

    def supports_discography(self) -> bool:
        return True

    def get_album(self, provider_id: str, session: AuthSession) -> Album | None:
        params = {"inc": "recordings+artist-credits", "fmt": "json"}
        resp = httpx.get(f"{_BASE}/release/{provider_id}", params=params, headers=_HEADERS, timeout=self.context.http_timeout)
        if resp.status_code == 200:
            return self._release_to_album(resp.json(), include_tracks=True)
        if resp.status_code != 404:
            resp.raise_for_status()
        # Fallback: provider_id potrebbe essere un release-group ID (da get_artist_albums)
        rg_resp = httpx.get(
            f"{_BASE}/release-group/{provider_id}",
            params={"inc": "releases+artist-credits", "fmt": "json"},
            headers=_HEADERS,
            timeout=self.context.http_timeout,
        )
        if rg_resp.status_code == 404:
            return None
        rg_resp.raise_for_status()
        releases = rg_resp.json().get("releases", [])
        if not releases:
            return None
        release_id = releases[0]["id"]
        rel_resp = httpx.get(f"{_BASE}/release/{release_id}", params=params, headers=_HEADERS, timeout=self.context.http_timeout)
        if rel_resp.status_code == 404:
            return None
        rel_resp.raise_for_status()
        return self._release_to_album(rel_resp.json(), include_tracks=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _recording_to_tracks(self, rec: dict[str, Any]) -> list[Track]:
        title = rec.get("title")
        if not title:
            return []

        artist_credits = rec.get("artist-credit", [])
        artist = self._artist_name(artist_credits)
        duration_ms = rec.get("length") or 0  # MusicBrainz returns milliseconds

        first_credit = next((c for c in artist_credits if isinstance(c, dict) and "artist" in c), None)
        artist_provider_id = first_credit["artist"]["id"] if first_credit else ""
        artists = [
            ArtistRef(name=c["artist"]["name"], provider=self.name, provider_id=c["artist"]["id"])
            for c in artist_credits if isinstance(c, dict) and "artist" in c and c["artist"].get("id")
        ]
        releases = self._unique_releases(rec.get("releases", []))
        if not releases:
            releases = [{}]

        tracks: list[Track] = []
        fallback_year = parse_year(rec.get("first-release-date", ""))
        for release in releases:
            album_provider_id = release.get("id", "")
            track = Track(
                title=title,
                artist=artist,
                album=release.get("title", ""),
                duration_ms=duration_ms,
                provider=self.name,
                provider_id=self._compose_track_provider_id(rec["id"], album_provider_id),
                artist_provider_id=artist_provider_id,
                album_provider_id=album_provider_id,
                year=parse_year(release.get("date", "")) or fallback_year,
            )
            track.artists = list(artists)
            track.add_id(Identifier(type=MBID, value=rec["id"]))

            for isrc in rec.get("isrcs", []):
                track.add_id(Identifier(type=ISRC, value=isrc))

            tracks.append(track)

        return tracks

    def _release_to_album(self, release: dict[str, Any], include_tracks: bool = False) -> Album | None:
        title = release.get("title")
        if not title:
            return None

        artist_credits = release.get("artist-credit", [])
        artist = self._artist_name(artist_credits)
        date = release.get("date", "")
        year = parse_year(date)
        first_credit = next((c for c in artist_credits if isinstance(c, dict) and "artist" in c), None)
        artist_provider_id = first_credit["artist"]["id"] if first_credit else ""

        album = Album(title=title, artist=artist, year=year,
                      track_count=release.get("track-count"),
                      provider=self.name, provider_id=release["id"],
                      artist_provider_id=artist_provider_id)
        album.artists = [
            ArtistRef(name=c["artist"]["name"], provider=self.name, provider_id=c["artist"]["id"])
            for c in artist_credits if isinstance(c, dict) and "artist" in c and c["artist"].get("id")
        ]
        album.add_id(Identifier(type=MBID, value=release["id"]))

        if include_tracks:
            for medium in release.get("media", []):
                for t in medium.get("tracks", []):
                    rec = t.get("recording") or {}
                    # Merge track-level fields as fallback when recording object is sparse
                    merged = {
                        "id": rec.get("id") or t.get("id"),
                        "title": rec.get("title") or t.get("title"),
                        "length": rec.get("length") or t.get("length"),
                        "artist-credit": rec.get("artist-credit") or t.get("artist-credit", []),
                        "isrcs": rec.get("isrcs", []),
                        "releases": [release],
                    }
                    for track in self._recording_to_tracks(merged):
                        track.track_number = t.get("position")
                        track.disc_number = medium.get("position")
                        album.tracks.append(track)
            if album.track_count is None and album.tracks:
                album.track_count = len(album.tracks)

        return album

    @staticmethod
    def _compose_track_provider_id(recording_id: str, release_id: str) -> str:
        return f"{recording_id}::{release_id}" if release_id else recording_id

    @staticmethod
    def _parse_track_provider_id(provider_id: str) -> tuple[str, str | None]:
        if "::" not in provider_id:
            return provider_id, None
        recording_id, release_id = provider_id.split("::", 1)
        return recording_id, release_id or None

    @staticmethod
    def _unique_releases(releases: list[dict]) -> list[dict]:
        unique: list[dict] = []
        seen_ids: set[str] = set()
        for release in releases:
            release_id = release.get("id", "")
            if release_id and release_id in seen_ids:
                continue
            if release_id:
                seen_ids.add(release_id)
            unique.append(release)
        return unique

    @staticmethod
    def _artist_name(credits: list) -> str:
        artist_credits = [c for c in credits if isinstance(c, dict) and "artist" in c]
        parts = []
        for i, c in enumerate(artist_credits):
            parts.append(c["artist"].get("name", ""))
            joinphrase = c.get("joinphrase", "")
            if joinphrase:
                parts.append(joinphrase)
            elif i < len(artist_credits) - 1:
                parts.append(", ")
        # Handle legacy format where credits may include plain strings
        if not parts:
            parts = [c for c in credits if isinstance(c, str)]
        return "".join(parts).strip()


_PRIMARY_TYPES = {"album": "album", "single": "single", "ep": "ep"}


def _release_group_type(rg: dict) -> str | None:
    """Tipo generico di un release group: i tipi secondari "Live" e
    "Compilation" prevalgono sul tipo primario."""
    secondary = {t.lower() for t in rg.get("secondary-types") or []}
    if "live" in secondary:
        return "live"
    if "compilation" in secondary:
        return "compilation"
    return _PRIMARY_TYPES.get((rg.get("primary-type") or "").lower())
