"""Navidrome (Subsonic API): browse the library, sync likes and playlists, verify what is in the library."""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx

from musizerr_sdk.auth import AccountIdentity, AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizerr_sdk.errors import RemotePlaylistGone
from musizerr_sdk.identifiers import ISRC, PROVIDER_ID, Identifier
from musizerr_sdk.interfaces import PROVIDER_API_VERSION, MetadataProvider, SyncCandidate, SyncProvider
from musizerr_sdk.models import Album, Artist, ArtistRef, LibraryItem, Track
from musizerr_sdk.permissions import Permissions
from musizerr_sdk.settings import ProviderSetting
from musizerr_sdk.text import parse_bitrate, parse_sample_rate, strip_version_suffix

# Content-Type -> estensione, solo per dare al file temporaneo un suffisso
# leggibile: ffmpeg/fpcalc riconoscono il formato dal CONTENUTO, non
# dall'estensione, quindi un content-type non mappato resta senza suffisso
# senza compromettere l'identificazione.
_CONTENT_TYPE_SUFFIX = {
    "audio/mpeg": ".mp3",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/opus": ".opus",
}

# Subsonic error code "the requested data was not found".
_SUBSONIC_NOT_FOUND = 70

# Pagina di search3 con query vuota: Navidrome (Subsonic esteso) la accetta
# come "elenca tutto" — songOffset/songCount fanno da paging. 500 è il
# massimo comunemente accettato dai server Subsonic per una singola pagina.
_LIBRARY_PAGE_SIZE = 500


def _isrcs(song: dict) -> list[str]:
    """The ISRCs of a Subsonic song (OpenSubsonic ``isrc``: a list, where a
    file tagged with several may give them in one entry separated by ";")."""
    return [code.strip() for entry in song.get("isrc") or [] for code in str(entry).split(";") if code.strip()]


def _bitrate_bps(song: dict) -> int:
    """Subsonic ``bitRate`` is in kbps; the host counts bit/s."""
    kbps = song.get("bitRate")
    return parse_bitrate(f"{kbps} kbps") if kbps else 0

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Credentials never go into a message or a log
# ----------------------------------------------------------------------
# A Subsonic call carries the user (u, and `username` for getUser), the salted token (t), the salt (s) and, on older
# servers, the password (p) in its query string, and httpx puts the whole URL in the message of an HTTPStatusError and in its own "HTTP Request"
# log line. Those texts reach the host's logs and, for an admin, the UI. They are redacted where they are made:
# the exception keeps its class, its status code and its response, only its text and the URLs it holds change, so the
# runner classifies it exactly as before.
_REDACTED = "redacted"
_CREDENTIAL_PARAMETER = re.compile(r"([?&](?:u|t|s|p|username)=)[^&\s'\"<>]*")
_AUTHORIZATION = re.compile(r"((?i:authorization)['\"]?\s*[:=]\s*['\"]?)[^\s'\",;]+(\s+[^\s'\",;]+)?")


def _redact(text: str) -> str:
    return _AUTHORIZATION.sub(rf"\1{_REDACTED}", _CREDENTIAL_PARAMETER.sub(rf"\1{_REDACTED}", text))


def _redacted_url(url: httpx.URL) -> httpx.URL:
    return httpx.URL(_redact(str(url)))


@contextmanager
def _no_credentials_in_errors():
    """Run a Subsonic request; an httpx error leaves it with its credentials redacted and its class, status and
    response intact."""
    try:
        yield
    except httpx.HTTPError as exc:
        _scrub(exc)
        raise


def _scrub(exc: httpx.HTTPError) -> None:
    exc.args = tuple(_redact(arg) if isinstance(arg, str) else arg for arg in exc.args)
    request = getattr(exc, "_request", None)
    if request is not None:
        exc._request = _scrubbed_request(request)  # noqa: SLF001
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "_request", None) is not None:
        response.request = _scrubbed_request(response.request)


def _scrubbed_request(request: httpx.Request) -> httpx.Request:
    return httpx.Request(request.method, _redacted_url(request.url))


class _RedactHttpxLog(logging.Filter):
    """httpx logs every request it makes ('HTTP Request: GET <url>') on its own logger."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = _redact(record.getMessage()), None
        return True


logging.getLogger("httpx").addFilter(_RedactHttpxLog())


class NavidromeProvider(MetadataProvider, SyncProvider):
    """
    Provider duale per Navidrome.
    Implementa MetadataProvider (browse libreria) e SyncProvider (trigger scan,
    verifica disponibilita, streaming).
    Modalita configurabile: "meta" (solo browse) o "full" (source of truth).
    """

    name = "navidrome"
    label = "Navidrome"
    provider_api_version = PROVIDER_API_VERSION
    # The Navidrome server is wherever the user says: the setting `url`, private and Tailscale addresses included.
    # It is the only destination of every request the provider makes (the Subsonic API, the download of a file for
    # fetch_partial). The stream and cover URLs it hands out are fetched by the host, not by the provider.
    permissions = Permissions(egress_from_settings=("url",))
    # The shared account indexes the library; each user's own account carries
    # their likes and their playlists.
    auth = AuthDefinition(flow=AuthFlow.CREDENTIALS, account_model=AccountModel.PERSONAL_WITH_SHARED)

    def config_schema(self) -> list[ProviderSetting]:
        return [
            ProviderSetting(key="url", type="string", default=None,
                            label="Server URL", required=True,
                            description="URL del server Navidrome (es. http://navidrome:4533)"),
            ProviderSetting(
                key="mode", type="enum", default=None,
                label="Modalita",
                required=True,
                options=("meta", "full"),
                description="meta = solo browse libreria. full = source of truth per lo stato download + rescan.",
            ),
        ]

    def validate_config(self, config: dict) -> bool:
        return bool(config.get("url")) and config.get("mode") in ("meta", "full")

    def test_connection(self, session: AuthSession) -> str | None:
        try:
            self._api("ping", session)
            return None
        except Exception as e:
            return str(e)

    def supports_login(self) -> bool:
        return True

    def identify_account(self, session: AuthSession) -> AccountIdentity | None:
        """The Navidrome user the credentials belong to (``getUser``: the
        server checks them, a wrong password is an error status). The id is the
        username the server reports."""
        username = session.extra.get("username", "")
        user = self._api("getUser", session, {"username": username}).get("user") or {}
        reported = user.get("username")
        return AccountIdentity(str(reported), None) if reported else None

    # ------------------------------------------------------------------
    # Subsonic auth helpers
    # ------------------------------------------------------------------

    def _subsonic_params(self, session: AuthSession) -> dict:
        password = session.extra.get("password", "")
        salt = secrets.token_hex(8)
        token = hashlib.md5((password + salt).encode()).hexdigest()
        return {
            "u": session.extra.get("username", ""),
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "musizerr",
            "f": "json",
        }

    def _base_url(self, session: AuthSession) -> str:
        config = self._get_config()
        return config.get("url", "").rstrip("/")

    def _get_config(self) -> dict:
        return dict(self.context.settings())

    # Salva le credenziali della sessione corrente per i converter
    # che generano cover URL (servono i parametri auth).
    _last_session_extra: dict = {}

    def _api(self, endpoint: str, session: AuthSession, params: dict | None = None) -> dict:
        """Esegue una chiamata Subsonic API e ritorna il payload."""
        self._last_session_extra = session.extra
        base = self._base_url(session)
        url = f"{base}/rest/{endpoint}"
        all_params = {**self._subsonic_params(session), **(params or {})}
        with _no_credentials_in_errors():
            resp = httpx.get(url, params=all_params, timeout=self.context.http_timeout)
            resp.raise_for_status()
        data = resp.json()
        sr = data.get("subsonic-response", {})
        if sr.get("status") != "ok":
            error = sr.get("error", {})
            logger.warning("Subsonic API error on %s: %s", endpoint, error)
            return {}
        return sr

    # ------------------------------------------------------------------
    # MetadataProvider — search
    # ------------------------------------------------------------------

    def search_tracks(self, query: str, session: AuthSession, limit: int = 20) -> list[Track]:
        sr = self._api("search3", session, {"query": query, "songCount": limit, "albumCount": 0, "artistCount": 0})
        result = sr.get("searchResult3", {})
        return [self._to_track(s) for s in result.get("song", [])]

    def search_albums(self, query: str, session: AuthSession, limit: int = 20) -> list[Album]:
        sr = self._api("search3", session, {"query": query, "albumCount": limit, "songCount": 0, "artistCount": 0})
        result = sr.get("searchResult3", {})
        return [self._to_album(a) for a in result.get("album", [])]

    def search_artists(self, query: str, session: AuthSession, limit: int = 20) -> list[Artist]:
        sr = self._api("search3", session, {"query": query, "artistCount": limit, "songCount": 0, "albumCount": 0})
        result = sr.get("searchResult3", {})
        return [self._to_artist(a) for a in result.get("artist", [])]

    # ------------------------------------------------------------------
    # MetadataProvider — get by ID
    # ------------------------------------------------------------------

    def get_track(self, provider_id: str, session: AuthSession) -> Track | None:
        sr = self._api("getSong", session, {"id": provider_id})
        song = sr.get("song")
        if not song:
            return None
        return self._to_track(song)

    def get_album(self, provider_id: str, session: AuthSession) -> Album | None:
        sr = self._api("getAlbum", session, {"id": provider_id})
        album_data = sr.get("album")
        if not album_data:
            return None
        return self._to_album_full(album_data)

    def get_artist(self, provider_id: str, session: AuthSession) -> Artist | None:
        sr = self._api("getArtist", session, {"id": provider_id})
        artist_data = sr.get("artist")
        if not artist_data:
            return None
        return self._to_artist(artist_data)

    def get_artist_albums(self, provider_id: str, session: AuthSession, limit: int = 50) -> list[Album]:
        sr = self._api("getArtist", session, {"id": provider_id})
        artist_data = sr.get("artist", {})
        return [self._to_album(a) for a in artist_data.get("album", [])]

    # ------------------------------------------------------------------
    # MetadataProvider — user library (starred tracks)
    # ------------------------------------------------------------------

    def supports_user_library(self) -> bool:
        return True

    def get_saved_tracks(self, session: AuthSession, limit: int = 50, offset: int = 0) -> list[Track]:
        sr = self._api("getStarred2", session)
        starred = sr.get("starred2", {})
        songs = starred.get("song", [])
        sliced = songs[offset:offset + limit]
        results = []
        for song in sliced:
            track = self._to_track(song)
            starred_at = song.get("starred")
            if starred_at:
                try:
                    # Naive UTC — matches datetimes round-tripped through SQLite,
                    # which drops tzinfo and would otherwise break comparisons.
                    track.saved_at = datetime.fromisoformat(starred_at.replace("Z", "+00:00")).replace(tzinfo=None)
                except (ValueError, TypeError):
                    pass
            results.append(track)
        return results

    # ------------------------------------------------------------------
    # SyncProvider
    # ------------------------------------------------------------------

    def trigger_scan(self, session: AuthSession) -> None:
        self._api("startScan", session)
        logger.info("Navidrome scan triggered")

    def find_candidates(self, track: Track, session: AuthSession) -> list[SyncCandidate]:
        # La ricerca per titolo "core" (senza coda di versione) recupera i
        # candidati da Navidrome; quale sia la traccia lo decide l'host.
        title_core = strip_version_suffix(track.title or "")
        sr = self._api("search3", session, {"query": title_core, "songCount": 20, "albumCount": 0, "artistCount": 0})
        songs = sr.get("searchResult3", {}).get("song", [])

        return [SyncCandidate(sync_id=str(song.get("id", "")), track=self._song_to_track(song)) for song in songs]

    def _song_to_track(self, song: dict) -> Track:
        """Mappa un risultato Subsonic 'song' su una Track di dominio."""
        track = Track(
            title=song.get("title", "") or "",
            artist=song.get("artist", "") or "",
            album=song.get("album", "") or "",
            duration_ms=int(song.get("duration") or 0) * 1000,
            year=song.get("year"),
            provider=self.name,
            provider_id=str(song.get("id", "")),
        )
        for isrc in _isrcs(song):
            track.add_id(Identifier(type=ISRC, value=isrc))
        return track

    def get_stream_url(self, sync_id: str, session: AuthSession) -> str | None:
        base = self._base_url(session)
        params = {**self._subsonic_params(session), "id": sync_id}
        return f"{base}/rest/stream?{urlencode(params)}"

    def supports_likes(self) -> bool:
        return True

    def like_track(self, sync_id: str, session: AuthSession) -> None:
        self._api("star", session, {"id": sync_id})

    def unlike_track(self, sync_id: str, session: AuthSession) -> None:
        self._api("unstar", session, {"id": sync_id})

    # ------------------------------------------------------------------
    # SyncProvider — playlists of the user's own account
    # ------------------------------------------------------------------

    def supports_playlists(self) -> bool:
        return True

    def _playlist_call(self, endpoint: str, session: AuthSession, params: dict) -> dict:
        """A Subsonic call whose failure matters: unlike ``_api`` (which turns an
        error status into an empty answer) it raises, ``RemotePlaylistGone`` when
        the server says the playlist is not there."""
        self._last_session_extra = session.extra
        url = f"{self._base_url(session)}/rest/{endpoint}"
        with _no_credentials_in_errors():
            resp = httpx.get(url, params={**self._subsonic_params(session), **params}, timeout=self.context.http_timeout)
            resp.raise_for_status()
        answer = resp.json().get("subsonic-response", {})
        if answer.get("status") != "ok":
            error = answer.get("error") or {}
            if error.get("code") == _SUBSONIC_NOT_FOUND:
                raise RemotePlaylistGone(error.get("message", ""))
            raise RuntimeError(f"Subsonic {endpoint} failed: {error.get('message') or error.get('code')}")
        return answer

    def create_playlist(self, name: str, sync_ids: list[str], session: AuthSession) -> str:
        answer = self._playlist_call("createPlaylist", session, {"name": name, "songId": list(sync_ids)})
        remote_id = (answer.get("playlist") or {}).get("id")
        if not remote_id:
            raise RuntimeError("Subsonic createPlaylist answered without the playlist")
        return str(remote_id)

    def update_playlist(self, remote_id: str, name: str, sync_ids: list[str], session: AuthSession) -> None:
        # createPlaylist with an id replaces the content of that playlist.
        self._playlist_call("createPlaylist", session, {"playlistId": remote_id, "songId": list(sync_ids)})
        self._playlist_call("updatePlaylist", session, {"playlistId": remote_id, "name": name})

    def delete_playlist(self, remote_id: str, session: AuthSession) -> None:
        self._playlist_call("deletePlaylist", session, {"id": remote_id})

    def list_library(self, session: AuthSession) -> list[LibraryItem]:
        """
        Elenca l'intera libreria via ``search3`` con query vuota, paginando
        con songOffset/songCount. Ogni 'song' Subsonic porta già ``path``
        (relativo alla music folder configurata su Navidrome — la STESSA
        cartella che lo storage provider attivo usa come base quando siamo in
        full-sync mode, vedi ``workers.maintenance.verify_full_sync``),
        ``suffix``/``bitRate``/``samplingRate``/``size``: nessun probe sui
        byte, solo i tag che il server ha già indicizzato.
        """
        items: list[LibraryItem] = []
        offset = 0
        while True:
            sr = self._api("search3", session, {
                "query": "",
                "songCount": _LIBRARY_PAGE_SIZE,
                "songOffset": offset,
                "albumCount": 0,
                "artistCount": 0,
            })
            songs = sr.get("searchResult3", {}).get("song", [])
            if not songs:
                break
            items.extend(self._song_to_library_item(s) for s in songs)
            if len(songs) < _LIBRARY_PAGE_SIZE:
                break
            offset += _LIBRARY_PAGE_SIZE
        return items

    def _song_to_library_item(self, song: dict) -> LibraryItem:
        return LibraryItem(
            sync_id=str(song.get("id", "")),
            path=song.get("path", "") or "",
            title=song.get("title", "") or "",
            artist=song.get("artist", "") or "",
            album=song.get("album", "") or "",
            year=song.get("year"),
            duration_ms=int(song.get("duration") or 0) * 1000,
            size=int(song.get("size") or 0),
            suffix=(song.get("suffix") or "").lower(),
            bitrate=_bitrate_bps(song),
            sample_rate=parse_sample_rate(song.get("samplingRate")),
        )

    def fetch_partial(
        self, sync_id: str, workspace: str, session: AuthSession, max_bytes: int | None = None,
    ) -> str | None:
        """
        Scarica (troncando a ``max_bytes`` se dato) il file ORIGINALE via
        Subsonic ``download`` — non ``stream``, che puo' transcodificare —
        cosi' il fingerprint gira sugli stessi byte davvero in libreria.
        Va per ``sync_id`` (l'unico modo affidabile: il ``path`` di
        ``list_library`` e' virtuale, ricostruito dai tag, e non e'
        detto punti al file reale — verificato dal vivo)."""
        base = self._base_url(session)
        params = {**self._subsonic_params(session), "id": sync_id}
        url = f"{base}/rest/download?{urlencode(params)}"

        name: str | None = None
        written = 0
        try:
            with _no_credentials_in_errors(), httpx.stream("GET", url, timeout=self.context.http_timeout) as resp:
                resp.raise_for_status()
                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if content_type in ("application/json", "text/xml"):
                    # Subsonic risponde con un errore strutturato invece del
                    # file audio (es. id sconosciuto) — mai un dedup "al buio".
                    logger.warning("Navidrome download id=%s ha risposto %s invece di audio", sync_id, content_type)
                    return None
                name = f"partial{_CONTENT_TYPE_SUFFIX.get(content_type, '')}"
                with open(Path(workspace) / name, "wb") as f:
                    for chunk in resp.iter_bytes(65536):
                        f.write(chunk)
                        written += len(chunk)
                        if max_bytes is not None and written >= max_bytes:
                            break
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("Navidrome fetch_partial fallito per sync_id=%s: %s", sync_id, exc)
            written = 0

        if written == 0:
            return None
        return name

    # ------------------------------------------------------------------
    # Converters
    # ------------------------------------------------------------------

    def _cover_url(self, cover_art_id: str | None, session: AuthSession) -> str | None:
        if not cover_art_id:
            return None
        # URL servito dall'host (che chiede a get_cover_url) invece dell'URL
        # assoluto del server Navidrome, spesso non raggiungibile dal browser
        # (es. host interno alla rete Docker).
        return self.context.cover_url(cover_art_id)

    def get_cover_url(self, cover_id: str, session: AuthSession) -> str | None:
        base = self._base_url(session)
        params = {**self._subsonic_params(session), "id": cover_id, "size": 300}
        return f"{base}/rest/getCoverArt?{urlencode(params)}"

    def _to_track(self, data: dict) -> Track:
        song_id = str(data["id"])
        track = Track(
            title=data.get("title", ""),
            artist=data.get("artist", ""),
            album=data.get("album", ""),
            duration_ms=int(data.get("duration", 0)) * 1000,
            cover_url=self._cover_url(data.get("coverArt"), AuthSession(flow=AuthFlow.CREDENTIALS, extra=self._last_session_extra)),
            stream_url=self.context.stream_url(song_id),
            provider=self.name,
            provider_id=song_id,
            artist_provider_id=str(data.get("artistId", "")),
            album_provider_id=str(data.get("albumId", "")),
            track_number=data.get("track"),
            disc_number=data.get("discNumber"),
        )
        track.add_id(Identifier(type=PROVIDER_ID, value=str(data["id"]), provider=self.name))
        for isrc in _isrcs(data):
            track.add_id(Identifier(type=ISRC, value=isrc))

        if data.get("year"):
            track.year = int(data["year"])

        if data.get("artistId"):
            track.artists = [
                ArtistRef(
                    name=data.get("artist", ""),
                    provider=self.name,
                    provider_id=str(data["artistId"]),
                )
            ]

        return track

    def _to_album(self, data: dict) -> Album:
        album = Album(
            title=data.get("name") or data.get("title", ""),
            artist=data.get("artist", ""),
            year=data.get("year"),
            track_count=data.get("songCount"),
            cover_url=self._cover_url(data.get("coverArt"), AuthSession(flow=AuthFlow.CREDENTIALS, extra=self._last_session_extra)),
            provider=self.name,
            provider_id=str(data["id"]),
            artist_provider_id=str(data.get("artistId", "")),
        )
        album.add_id(Identifier(type=PROVIDER_ID, value=str(data["id"]), provider=self.name))
        if data.get("artistId"):
            album.artists = [
                ArtistRef(
                    name=data.get("artist", ""),
                    provider=self.name,
                    provider_id=str(data["artistId"]),
                )
            ]
        return album

    def _to_album_full(self, data: dict) -> Album:
        album = self._to_album(data)
        for s in data.get("song", []):
            album.tracks.append(self._to_track(s))
        return album

    def _to_artist(self, data: dict) -> Artist:
        artist = Artist(
            name=data.get("name", ""),
            provider=self.name,
            provider_id=str(data["id"]),
            cover_url=self._cover_url(data.get("coverArt"), AuthSession(flow=AuthFlow.CREDENTIALS, extra=self._last_session_extra)),
        )
        artist.add_id(Identifier(type=PROVIDER_ID, value=str(data["id"]), provider=self.name))
        return artist

