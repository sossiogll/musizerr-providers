"""Lidarr: asks a Lidarr server to get an album (the track's, or a whole one) and follows the delegation."""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import httpx

from musizerr_sdk import delegation as ds
from musizerr_sdk.auth import AccountModel, ApiKeyConfig, AuthDefinition, AuthFlow, AuthSession
from musizerr_sdk.delegation import DelegationStatus, FileRef
from musizerr_sdk.errors import DelegateHoldsRelease, DownloadDelegated
from musizerr_sdk.identifiers import Identifier
from musizerr_sdk.interfaces import PROVIDER_API_VERSION, DownloadProvider
from musizerr_sdk.models import Album, Track
from musizerr_sdk.permissions import Permissions
from musizerr_sdk.settings import ProviderSetting
from musizerr_sdk.text import LOSSLESS_CODECS, parse_bitrate, parse_sample_rate

logger = logging.getLogger(__name__)

# Eventi Lidarr (Connect → Webhook) che meritano una riconciliazione. Vive qui,
# non in core/ o routes/: è vocabolario specifico di questo sistema esterno.
_RELEVANT_PUSH_EVENTS = {
    "Download", "AlbumDownload", "TrackFileImported", "Import",
    "ImportComplete", "ImportFailure", "Rename", "Retag", "Upgrade",
}


def _extract_album_id(payload: dict) -> int | None:
    album = payload.get("album") or {}
    for key in ("id", "albumId"):
        val = album.get(key) if isinstance(album, dict) else None
        if isinstance(val, int):
            return val
    if isinstance(payload.get("albumId"), int):
        return payload["albumId"]
    return None


_MB_BASE = "https://musicbrainz.org/ws/2"
_MB_HEADERS = {"User-Agent": "musizerr/1.0 (https://github.com/sossiogll/musizerr)"}


# ── MusicBrainz lookup ────────────────────────────────────────────────────────

class MusicBrainzUnavailable(Exception):
    """MusicBrainz did not answer (network, timeout) or answered 429/5xx: says
    nothing about the track. Raised through the provider so the host retries
    the download later instead of failing it for good."""


def _musicbrainz(path: str, params: dict, timeout: int) -> dict | None:
    """GET on the MusicBrainz API: the JSON, None for any other refusal
    (4xx: the query itself is wrong), ``MusicBrainzUnavailable`` when the
    service is not available right now."""
    try:
        r = httpx.get(f"{_MB_BASE}{path}", params=params, headers=_MB_HEADERS, timeout=timeout)
    except httpx.RequestError as exc:
        raise MusicBrainzUnavailable(f"MusicBrainz unreachable: {exc}") from exc
    if r.status_code == 429 or r.status_code >= 500:
        raise MusicBrainzUnavailable(f"MusicBrainz answered {r.status_code}")
    try:
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPStatusError, ValueError) as exc:
        logger.debug("MusicBrainz %s refused: %s", path, exc)
        return None

def _find_release_group_mbid_for_track(title: str, artist: str, album: str = "", *, timeout: int) -> Optional[str]:
    """
    Cerca su MusicBrainz il release-group MBID partendo da una traccia.
    Naviga: recording → release → release-group per ottenere l'ID che Lidarr accetta.
    """
    query = f'recording:"{title}" AND artist:"{artist}"'
    if album:
        query += f' AND release:"{album}"'

    data = _musicbrainz("/recording", {"query": query, "limit": 5, "fmt": "json"}, timeout)
    if data is None:
        return None

    for recording in data.get("recordings", []):
        for release in recording.get("releases", []):
            mbid = release.get("release-group", {}).get("id")
            if mbid:
                logger.info("MusicBrainz: track '%s' → release-group MBID=%s", title, mbid)
                return mbid

    logger.warning("MusicBrainz: no release-group MBID found for track '%s' by '%s'", title, artist)
    return None


def _find_release_group_mbid_for_album(title: str, artist: str, *, timeout: int) -> Optional[str]:
    """
    Cerca su MusicBrainz il release-group MBID partendo dal titolo album.
    Interroga /release-group direttamente — più preciso per dispatch album-level.
    """
    query = f'release:"{title}" AND artist:"{artist}"'

    data = _musicbrainz("/release-group", {"query": query, "limit": 3, "fmt": "json"}, timeout)
    if data is None:
        return None

    groups = data.get("release-groups", [])
    if groups:
        mbid = groups[0].get("id")
        if mbid:
            logger.info("MusicBrainz: album '%s' by '%s' → release-group MBID=%s", title, artist, mbid)
            return mbid

    logger.warning("MusicBrainz: no release-group MBID found for album '%s' by '%s'", title, artist)
    return None


# ── Lidarr API client ─────────────────────────────────────────────────────────

class LidarrUnreachable(Exception):
    """
    Lidarr non ha risposto affatto (rete/DNS/timeout) — diverso da una
    risposta HTTP di errore (4xx/5xx: quella significa che il sistema ha
    risposto, solo con un esito negativo). Qui non sappiamo NULLA dello
    stato reale: nessun chiamante deve tradurre questo in un esito inventato
    (es. "in corso" o "fallito").
    """


# The statuses that say "something between us and Lidarr did not get an answer", not "Lidarr answered no": a
# reverse proxy in front of it (502, 503, 504) and, in the sandbox, the egress proxy of the gateway, which answers a plain-HTTP
# request to an address that does not respond with 502 Bad Gateway (an HTTPS one fails the CONNECT and is a RequestError).
# Without this an unreachable Lidarr would read as a definitive "not found" in the sandbox.
_NOT_ANSWERED = frozenset({502, 503, 504})


class LidarrAlbumExists(Exception):
    """``POST /album`` answered 409 Conflict: the album is already in Lidarr
    (added meanwhile, e.g. by the download of another track of the same
    album). Not a failure: the album is looked up and the dispatch goes on
    as for an album already there."""


def _unless_not_answered(exc: httpx.HTTPStatusError, method: str, path: str) -> None:
    """Raise ``LidarrUnreachable`` for a status that is not Lidarr's own answer (see ``_NOT_ANSWERED``); return for any other."""
    if exc.response.status_code in _NOT_ANSWERED:
        logger.error("Lidarr %s %s unreachable: %s", method, path, exc)
        raise LidarrUnreachable(str(exc)) from exc


class LidarrNotConfigured(Exception):
    """The provider has no Lidarr URL: there is nothing to talk to. Said with a reason, never swallowed."""


class LidarrRejected(httpx.HTTPStatusError):
    """Lidarr ANSWERED a write with an error status (a validation failure, a refused key, an error of its own),
    and ``str(exc)`` is the reason it gave. It is an ``httpx.HTTPStatusError`` on purpose: the runner classifies
    the failure by the status of ``response`` (401/403 refused credentials, 5xx an error upstream, any other
    4xx a refusal), and the host shows the administrator this message."""


_REASON_MAX = 500
_TOKEN_PARAM = re.compile(r"(?P<lead>[?&;]token=)[^&;#\s'\"<>]*", re.IGNORECASE)


def _hide_callback(text: str, callback_url: str) -> str:
    """``text`` without the callback URL's token. The URL carries the shared token of Musizerr's inbound webhook,
    and Lidarr (or httpx) may quote it back in a message: it never leaves this provider."""
    secrets = {value for value in parse_qs(urlsplit(callback_url).query).get("token", []) if value}
    for secret in secrets:
        text = text.replace(secret, "***")
    return _TOKEN_PARAM.sub(lambda m: m.group("lead") + "***", text)


def _lidarr_reason(response: httpx.Response) -> str:
    """What Lidarr says is wrong, from the body of an error answer: a list of validation failures
    (``[{"propertyName": "Url", "errorMessage": "..."}]``), or an object with a ``message``; the status text when
    it says nothing readable. Only the message fields are read: ``attemptedValue`` and the rest of a validation
    failure echo what was sent, the callback URL with its token among it."""
    try:
        body = response.json()
    except ValueError:
        body = None
    items = body if isinstance(body, list) else [body]
    reasons: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        message = item.get("errorMessage") or item.get("message")
        if not isinstance(message, str) or not message.strip():
            continue
        prop = item.get("propertyName")
        reason = f"{prop}: {message.strip()}" if isinstance(prop, str) and prop else message.strip()
        if reason not in reasons:
            reasons.append(reason)
    return "; ".join(reasons)[:_REASON_MAX] or response.reason_phrase or "no reason given"


class _LidarrClient:
    def __init__(self, base_url: str, api_key: str, timeout: int) -> None:
        self._base = base_url.rstrip("/") + "/api/v1"
        self._key = api_key
        self._timeout = timeout

    @property
    def _headers(self) -> dict:
        # La chiave viaggia nell'header X-Api-Key, supportato da Lidarr, e mai
        # come parametro dell'URL: un URL finisce nei messaggi di errore di
        # httpx e quindi nei log, e con esso la credenziale.
        return {"X-Api-Key": self._key}

    def _get(self, path: str, params: dict | None = None, *, optional: bool = False) -> list | dict | None:
        try:
            r = httpx.get(f"{self._base}{path}", params=params or None,
                          headers=self._headers, timeout=self._timeout)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            _unless_not_answered(exc, "GET", path)
            # Lidarr ha risposto, con un esito negativo (4xx/5xx) — non e'
            # un'irraggiungibilita', e' una risposta legittima (es. 404).
            # Un endpoint `optional` che non esiste in questa versione di
            # Lidarr e' atteso, non un errore: il chiamante ha un ripiego.
            if optional and exc.response.status_code == 404:
                logger.debug("Lidarr GET %s not available in this version", path)
            else:
                logger.error("Lidarr GET %s failed: %s", path, exc)
            return None
        except httpx.RequestError as exc:
            # Nessuna risposta arrivata affatto: rete giu', DNS, timeout.
            logger.error("Lidarr GET %s unreachable: %s", path, exc)
            raise LidarrUnreachable(str(exc)) from exc
        except ValueError as exc:  # JSON malformato: Lidarr ha risposto ma in modo illeggibile. Non catturare di piu': un errore del NOSTRO codice deve emergere, non passare per un fallimento di Lidarr.
            logger.error("Lidarr GET %s failed: %s", path, exc)
            return None

    def _post(self, path: str, body: dict, *, conflict: type[Exception] | None = None) -> dict | None:
        """POST: il JSON della risposta, None se Lidarr l'ha rifiutato. Con
        ``conflict``, un 409 solleva quell'eccezione invece di None."""
        try:
            r = httpx.post(
                f"{self._base}{path}",
                headers=self._headers,
                json=body,
                timeout=self._timeout,
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            _unless_not_answered(exc, "POST", path)
            if conflict is not None and exc.response.status_code == 409:
                logger.info("Lidarr POST %s: already exists (409)", path)
                raise conflict(exc.response.text[:200]) from exc
            logger.error("Lidarr POST %s failed: %s", path, exc)
            return None
        except httpx.RequestError as exc:
            logger.error("Lidarr POST %s unreachable: %s", path, exc)
            raise LidarrUnreachable(str(exc)) from exc
        except ValueError as exc:  # JSON malformato: Lidarr ha risposto ma in modo illeggibile. Non catturare di piu': un errore del NOSTRO codice deve emergere, non passare per un fallimento di Lidarr.
            logger.error("Lidarr POST %s failed: %s", path, exc)
            return None

    def _put(self, path: str, body: dict) -> dict | None:
        try:
            r = httpx.put(
                f"{self._base}{path}",
                headers=self._headers,
                json=body,
                timeout=self._timeout,
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            _unless_not_answered(exc, "PUT", path)
            logger.error("Lidarr PUT %s failed: %s", path, exc)
            return None
        except httpx.RequestError as exc:
            logger.error("Lidarr PUT %s unreachable: %s", path, exc)
            raise LidarrUnreachable(str(exc)) from exc
        except ValueError as exc:  # JSON malformato: Lidarr ha risposto ma in modo illeggibile. Non catturare di piu': un errore del NOSTRO codice deve emergere, non passare per un fallimento di Lidarr.
            logger.error("Lidarr PUT %s failed: %s", path, exc)
            return None

    def notification_call(self, method: str, path: str, body: dict | None = None, *, callback_url: str = "") -> list | dict:
        """A call on the notifications (Connect connections) that says WHY it failed instead of answering None:
        the JSON of the answer when Lidarr accepted it; ``LidarrUnreachable`` when it did not answer;
        ``LidarrRejected`` with Lidarr's own reason when it answered no. ``callback_url`` is hidden in every
        message (and in the log)."""
        send = {"GET": httpx.get, "POST": httpx.post, "PUT": httpx.put}[method]
        kwargs: dict = {"headers": self._headers, "timeout": self._timeout}
        if body is not None:
            kwargs["json"] = body
        try:
            r = send(f"{self._base}{path}", **kwargs)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in _NOT_ANSWERED:
                message = _hide_callback(f"Lidarr unreachable: {exc}", callback_url)
                logger.error("Lidarr %s %s: %s", method, path, message)
                raise LidarrUnreachable(message) from exc
            status = exc.response.status_code
            reason = _hide_callback(_lidarr_reason(exc.response), callback_url)
            if status in (401, 403):
                message = f"Lidarr refused the API key ({status})"
            else:
                message = f"Lidarr refused the request ({status}): {reason}"
            logger.error("Lidarr %s %s: %s", method, path, message)
            raise LidarrRejected(message, request=exc.request, response=exc.response) from None
        except httpx.RequestError as exc:
            message = _hide_callback(f"Lidarr unreachable: {exc}", callback_url)
            logger.error("Lidarr %s %s: %s", method, path, message)
            raise LidarrUnreachable(message) from None
        try:
            return r.json()
        except ValueError:
            return {}  # accepted, with a body that is not JSON: Lidarr said yes, which is all the caller needs

    def test_connection(self) -> tuple[bool, str]:
        try:
            data = self._get("/system/status")
        except LidarrUnreachable as exc:
            return False, f"Lidarr unreachable: {exc}"
        if data and isinstance(data, dict):
            return True, data.get("version", "?")
        return False, "No response from Lidarr"

    def _first_id(self, path: str) -> int:
        result = self._get(path)
        if isinstance(result, list) and result:
            return result[0].get("id", 1)
        return 1

    def _first_root_folder(self) -> str:
        result = self._get("/rootfolder")
        if isinstance(result, list) and result:
            return result[0].get("path", "/music")
        return "/music"

    # Il path di DISPATCH (search_by_mbid, album_by_foreign_id, add_album)
    # NON cattura LidarrUnreachable: un Lidarr che non risponde non dice che
    # l'album non c'è. Si propaga fino al resolver, per cui un'eccezione è un
    # errore transitorio (contratto DownloadProvider: None = "non ce l'ho",
    # definitivo; raise = sistema non raggiungibile, si ritenta più tardi).

    def search_by_mbid(self, mbid: str) -> dict | None:
        """Cerca un album in Lidarr per release-group MBID. None se Lidarr ha
        risposto senza trovarlo; solleva ``LidarrUnreachable`` se non ha
        risposto."""
        results = self._get("/search", {"term": f"lidarr:{mbid}"})
        if not isinstance(results, list):
            return None
        for r in results:
            album = r.get("album")
            if album:
                return album
        return None

    def add_album(self, album_info: dict) -> dict | None:
        """Aggiunge l'album: l'album creato, None se Lidarr l'ha rifiutato.
        Solleva ``LidarrAlbumExists`` se c'è già (409) e ``LidarrUnreachable``
        se Lidarr non risponde."""
        quality_profile_id = self._first_id("/qualityProfile")
        metadata_profile_id = self._first_id("/metadataProfile")
        root_folder = self._first_root_folder()
        artist = album_info.get("artist", {})
        artist_name = artist.get("artistName", "Unknown Artist")
        payload = {
            "title": album_info.get("title", ""),
            "foreignAlbumId": album_info.get("foreignAlbumId", ""),
            "monitored": True,
            "anyReleaseOk": True,
            "profileId": quality_profile_id,
            "artistId": artist.get("id", 0),
            "artist": {
                "foreignArtistId": artist.get("foreignArtistId", ""),
                "path": f"{root_folder}/{artist_name}",
                "qualityProfileId": quality_profile_id,
                "metadataProfileId": metadata_profile_id,
                "monitored": True,
                "monitorNewItems": "none",
                "rootFolderPath": root_folder,
                "tags": [],
            },
            "addOptions": {"searchForNewAlbum": True},
        }
        return self._post("/album", payload, conflict=LidarrAlbumExists)

    # --- Riconciliazione (stato/file di un album delegato) ---
    #
    # Nemmeno le funzioni sotto catturano LidarrUnreachable: album_by_foreign_id
    # serve al dispatch (vedi sopra), le altre sono chiamate da poll_delegation, che deve
    # poter propagare "non ho potuto determinare lo stato" al chiamante
    # generico (workers.maintenance.reconcile_delegation) invece di far
    # finta che una risposta vuota significhi "nessun evento" — vedi la nota
    # sul contratto in musizerr_sdk.interfaces.

    def album_by_foreign_id(self, foreign_album_id: str) -> dict | None:
        """L'album già in Lidarr con questo id esterno, None se non c'è (path
        di dispatch: un'irraggiungibilità si propaga, vedi sopra)."""
        result = self._get("/album", {"foreignAlbumId": foreign_album_id})
        if isinstance(result, list) and result:
            return result[0]
        return None

    def album_status(self, album_id: int) -> dict | None:
        result = self._get(f"/album/{album_id}")
        return result if isinstance(result, dict) else None

    def queue_records(self) -> list[dict]:
        result = self._get("/queue", {"pageSize": 200, "includeUnknownArtistItems": True})
        if isinstance(result, dict):
            return result.get("records", []) or []
        return result if isinstance(result, list) else []

    def history_for_album(self, album_id: int) -> list[dict]:
        result = self._get("/history/album", {"albumId": album_id}, optional=True)
        if isinstance(result, list):
            return result
        # fallback: history paginata filtrata lato client
        result = self._get("/history", {"pageSize": 100, "sortKey": "date", "sortDirection": "descending"})
        records = result.get("records", []) if isinstance(result, dict) else []
        return [r for r in records if r.get("albumId") == album_id]

    def track_files(self, album_id: int) -> list[dict]:
        result = self._get("/trackFile", {"albumId": album_id})
        return result if isinstance(result, list) else []

    def tracks_for_album(self, album_id: int) -> list[dict]:
        """
        Elenco delle tracce Lidarr (titolo, durata, presenza file) per
        l'album — usato dal giudizio PER TRACCIA di poll_delegation (vedi il
        contratto in musizerr_sdk.interfaces): non cattura LidarrUnreachable per
        lo stesso motivo di track_files/album_status/queue_records/
        history_for_album, chiamate solo dal poll.
        """
        result = self._get("/track", {"albumId": album_id})
        return result if isinstance(result, list) else []

    def delete_track_file(self, track_file_id: int) -> bool:
        try:
            r = httpx.delete(
                f"{self._base}/trackFile/{track_file_id}",
                headers=self._headers, timeout=self._timeout,
            )
            r.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Lidarr DELETE trackFile/%s failed: %s", track_file_id, exc)
            return False

    def command(self, name: str, **kwargs) -> dict | None:
        return self._post("/command", {"name": name, **kwargs})

    def monitor_album(self, album_id: int) -> dict | list | None:
        return self._put("/album/monitor", {"albumIds": [album_id], "monitored": True})


# ── Provider ──────────────────────────────────────────────────────────────────

class LidarrDownloadProvider(DownloadProvider):
    name = "lidarr"
    label = "Lidarr"
    provider_api_version = PROVIDER_API_VERSION
    # Two destinations. The Lidarr server is wherever the user says: the setting `url` (it is also what the
    # sessions carry as `extra["url"]`), private and Tailscale addresses included. And MusicBrainz, the fixed
    # public host _musicbrainz() asks for the release group of a track before the album is sent to Lidarr.
    permissions = Permissions(egress=("musicbrainz.org:443",), egress_from_settings=("url",))

    # Lidarr non supporta download per recording MBID (registrations MBID ≠ release-group MBID).
    # Usa esclusivamente download_fuzzy() per tracce e dispatch_album() per album.
    supported_identifier_types: set[str] = set()

    # Lidarr è un fallback di ultimo livello — non abilitato by default
    enabled_by_default = False

    # Cerca l'album dai metadati della traccia (MusicBrainz → Lidarr).
    supports_fuzzy = True

    # The instance's own Lidarr server: one key, managed by an admin.
    auth = AuthDefinition(
        flow=AuthFlow.API_KEY,
        account_model=AccountModel.SYSTEM,
        api_key=ApiKeyConfig(query_param="apikey"),
    )

    def config_schema(self) -> list[ProviderSetting]:
        return [
            ProviderSetting(
                key="url", type="string", default=None,
                label="Lidarr URL",
                description="URL base della tua istanza Lidarr (es. http://localhost:8686).",
            ),
        ]

    def validate_config(self, config: dict) -> bool:
        return bool(config.get("url"))

    def test_connection(self, session: AuthSession) -> str | None:
        client = self._client(session)
        if client is None:
            return "Lidarr URL not configured"
        ok, info = client.test_connection()
        if ok:
            return None
        return f"Lidarr connection failed: {info}"

    # ------------------------------------------------------------------
    # Download per singola traccia (fuzzy — Fase 2 del resolver)
    # ------------------------------------------------------------------

    def download_by_id(self, identifier: Identifier, workspace: str, session: AuthSession) -> str | None:
        # Mai chiamato: supported_identifier_types è vuoto.
        return None

    def download_fuzzy(self, track: Track, workspace: str, session: AuthSession) -> str | None:
        """
        Cerca il release-group MBID su MusicBrainz e invia l'album a Lidarr.
        Solleva DownloadDelegated se Lidarr accetta la richiesta.
        """
        mbid = _find_release_group_mbid_for_track(
            track.title, track.artist, track.album or "", timeout=self.context.http_timeout,
        )
        if not mbid:
            logger.warning(
                "Lidarr fuzzy: no release-group MBID found for '%s' by '%s' — skipping",
                track.title, track.artist,
            )
            return None
        return self._dispatch_mbid(
            mbid,
            label=f"track '{track.title}' by '{track.artist}'",
            session=session,
            track=track,
        )

    # ------------------------------------------------------------------
    # Download album-level (chiamato direttamente dal route /download/album)
    # ------------------------------------------------------------------

    def dispatch_album(self, album: Album, session: AuthSession) -> None:
        """
        Dispatcha l'intero album a Lidarr con una singola richiesta.
        Solleva DownloadDelegated se Lidarr accetta, ValueError se non trova il MBID.
        """
        mbid = _find_release_group_mbid_for_album(album.title, album.artist, timeout=self.context.http_timeout)
        if not mbid:
            raise ValueError(
                f"MusicBrainz: release-group MBID not found for '{album.title}' by '{album.artist}'"
            )
        self._dispatch_mbid(mbid, label=f"album '{album.title}' by '{album.artist}'", session=session)

    # ------------------------------------------------------------------
    # Riconciliazione della delega (Lidarr → download_states generici)
    # ------------------------------------------------------------------

    def poll_delegation(self, handle: str, session: AuthSession):
        """
        Vedi il contratto in musizerr_sdk.interfaces: se Lidarr non risponde
        affatto (rete/timeout), le chiamate sottostanti (history_for_album/
        album_status/queue_records/tracks_for_album) sollevano
        LidarrUnreachable — qui NON viene catturata di proposito, si
        propaga al chiamante generico (workers.maintenance.reconcile_delegation),
        che la tratta come transitoria. Prima di questo fix un'irraggiungibilità
        totale produceva silenziosamente lo stesso DelegationStatus(DOWNLOADING)
        di un album genuinamente in corso — un'interruzione di rete non
        distinguibile da un download che avanza.

        Giudica l'INTERO album (history/queue/statistiche). Il giudizio PER
        TRACCIA (l'album ha già il file della traccia richiesta, anche se il
        resto è incompleto) è dell'host, che lo prende prima di questo poll con
        i dati di ``list_delegated_tracks``.
        """
        client = self._client(session)
        if client is None:
            return None
        try:
            album_id = int(handle)
        except (TypeError, ValueError):
            return None

        album = client.album_status(album_id) or {}

        # 1) history: esito definitivo (import riuscito / fallito) per
        # l'INTERO album.
        history = client.history_for_album(album_id)
        events = {h.get("eventType") for h in history}
        if events & {"downloadFailed"}:
            return DelegationStatus(state=ds.FAILED, detail="Lidarr download failed")

        # 2) album stats: quante tracce sono ormai su disco
        stats = album.get("statistics", {}) or {}
        track_count = stats.get("trackCount") or 0
        file_count = stats.get("trackFileCount") or 0
        fully_imported = track_count > 0 and file_count >= track_count

        # 3) queue: fase in corso + conflitti di import
        queue = [q for q in client.queue_records() if _q_album_id(q) == album_id]
        state = None
        for q in queue:
            tstate = (q.get("trackedDownloadState") or "").lower()
            tstatus = (q.get("trackedDownloadStatus") or "").lower()
            messages = " ".join(
                m.get("title", "") + " " + " ".join(m.get("messages", []))
                for m in (q.get("statusMessages") or [])
            ).lower()
            if tstatus in ("warning", "error") and ("already" in messages or "exist" in messages):
                # Conflitto di import (file già presenti). La risoluzione per
                # qualità avviene lato reconcile (metadata-level); qui segnaliamo.
                return DelegationStatus(state=ds.CONFLICT, detail=messages[:200])
            if tstate in ("importpending", "importing"):
                state = ds.IMPORTING
            elif tstate in ("downloading", "queued", "delay", "downloadclientunavailable"):
                state = state or ds.DOWNLOADING

        if fully_imported and not queue:
            return DelegationStatus(state=ds.DOWNLOADED)
        # Segnale REALE di attività: un item è effettivamente in coda lato
        # Lidarr → genuinamente in corso (vedi contratto: primo esito non
        # terminale, "il sistema ha risposto con un segnale di attività").
        if state:
            return DelegationStatus(state=state)
        if events & {"trackFileImported", "albumImported", "downloadFolderImported"} and fully_imported:
            return DelegationStatus(state=ds.DOWNLOADED)
        # Nessun item in coda, nessun file corrispondente: Lidarr HA risposto
        # ma non riporta alcuna attività reale (vedi contratto — secondo
        # esito non terminale, "un esito reale, non ignoranza"). NON si
        # inventa "downloading": si torna un non-terminale onesto, così il
        # max-age del chiamante generico può eventualmente marcarlo failed.
        return DelegationStatus(state=ds.DISPATCHED, detail="no active Lidarr queue item and no matching file yet")

    def list_delegated_tracks(self, handle: str, session: AuthSession):
        """Le tracce dell'album delegato che Lidarr ha GIÀ su disco: dati, nessun
        giudizio (vedi il contratto). L'artista delle tracce Lidarr non è esposto
        dalla risorsa ``/track`` (solo titolo e durata): si usa l'artista dell'ALBUM già
        risolto. L'album resta vuoto: il titolo che Lidarr dà al rilascio può
        differire nella forma da quello del metadata provider che ha richiesto
        il download (es. "Greatest Hits" vs "Greatest Hits (Remastered)"), ma
        il rilascio è lo stesso per costruzione (è quello scelto al momento
        della delega), e questo lo sa l'host."""
        client = self._client(session)
        if client is None:
            return []
        try:
            album_id = int(handle)
        except (TypeError, ValueError):
            return []

        album = client.album_status(album_id) or {}
        return _tracks_with_file(client.tracks_for_album(album_id), album)

    def list_delegated_files(self, handle: str, session: AuthSession):
        client = self._client(session)
        if client is None:
            return []
        try:
            album_id = int(handle)
        except (TypeError, ValueError):
            return []

        refs = []
        for tf in client.track_files(album_id):
            media = tf.get("mediaInfo", {}) or {}
            codec = (media.get("audioCodec") or "").lower()
            quality_name = ((tf.get("quality", {}) or {}).get("quality", {}) or {}).get("name", "").lower()
            lossless = codec in LOSSLESS_CODECS or "flac" in quality_name or "alac" in quality_name
            # Lidarr riporta questi due campi come "audioBitRate"/"audioSampleRate"
            # (R maiuscola nel primo) — es. {"audioBitRate": "320 kbps",
            # "audioSampleRate": "44.1kHz"}. Tollera anche il casing storico
            # sbagliato usato qui in precedenza ("audioBitrate", r minuscola,
            # che non esiste MAI in una risposta reale — sempre 0/sconosciuto).
            refs.append(FileRef(
                path=tf.get("path", ""),
                quality_lossless=lossless,
                bitrate=parse_bitrate(_media_value(media, "audioBitRate", "audioBitrate")),
                sample_rate=parse_sample_rate(_media_value(media, "audioSampleRate", "audioSamplerate")),
                external_id=str(tf.get("id")) if tf.get("id") is not None else None,
            ))
        return refs

    def delete_file(self, ref, session: AuthSession) -> bool:
        client = self._client(session)
        if client is None or not ref.external_id:
            return False
        try:
            return client.delete_track_file(int(ref.external_id))
        except (TypeError, ValueError):
            return False

    def request_rescan(self, session: AuthSession) -> None:
        client = self._client(session)
        if client is None:
            return None
        try:
            client.command("RescanFolders")
        except LidarrUnreachable as exc:
            logger.warning("Lidarr request_rescan unreachable: %s", exc)

    # ------------------------------------------------------------------
    # Push da Lidarr (Connect → Webhook)
    # ------------------------------------------------------------------

    def delegation_event_handle(self, payload: dict) -> str | None:
        """
        Interpreta una notifica Lidarr Connect. ``payload`` è NON FIDATO
        (arriva via rete da Lidarr): si leggono solo eventType e l'id
        strutturato dell'album, mai altro contenuto come istruzione.
        """
        if not isinstance(payload, dict):
            return None
        event = str(payload.get("eventType", ""))
        if event not in _RELEVANT_PUSH_EVENTS:
            return None
        album_id = _extract_album_id(payload)
        if album_id is None:
            return None
        return str(album_id)

    def register_push_endpoint(self, callback_url: str, session: AuthSession) -> str | None:
        """
        Auto-registra musizerr come Connection (Webhook) dentro Lidarr.
        Idempotente: Lidarr rifiuta un secondo POST con lo stesso 'name', quindi
        se la Connection "musizerr" esiste già la aggiorniamo (PUT) con l'URL
        corrente invece di duplicarla.

        Returns "created" or "updated". Raises when it did not work, with the reason: Lidarr tests the callback
        address when it saves the connection and answers 400 with a list of validation failures when it cannot
        reach it ("Unable to send test message"), which is the usual failure and is told as Lidarr said it;
        ``LidarrRejected`` for a refusal (the callback token is hidden in the message), ``LidarrUnreachable`` when
        Lidarr does not answer, ``LidarrNotConfigured`` without a URL. It never answers None for a failure.
        """
        client = self._client(session)
        if client is None:
            raise LidarrNotConfigured("Lidarr URL not configured")

        payload = {
            "onReleaseImport": True,
            "onUpgrade": True,
            "onImportFailure": True,
            "onHealthIssue": False,
            "name": "musizerr",
            "implementation": "Webhook",
            "configContract": "WebhookSettings",
            "fields": [
                {"name": "url", "value": callback_url},
                {"name": "method", "value": 1},  # POST
            ],
        }
        listed = client.notification_call("GET", "/notification", callback_url=callback_url)
        existing = next(
            (n for n in (listed if isinstance(listed, list) else []) if isinstance(n, dict) and n.get("name") == "musizerr"),
            None,
        )
        if existing:
            payload["id"] = existing["id"]
            client.notification_call("PUT", f"/notification/{existing['id']}", payload, callback_url=callback_url)
            return "updated"
        client.notification_call("POST", "/notification", payload, callback_url=callback_url)
        return "created"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _dispatch_mbid(self, mbid: str, label: str, session: AuthSession, track: Track | None = None) -> str | None:
        """
        Cerca l'album in Lidarr per release-group MBID e lo aggiunge se non presente.
        Solleva DownloadDelegated su successo. Ritorna None se non trovato nel catalogo.
        Se l'album c'è già, vedi ``_dispatch_existing``.
        """
        client = self._client(session)
        if client is None:
            logger.warning("Lidarr: URL not configured — skipping dispatch for %s", label)
            return None

        logger.info("Lidarr: searching catalog for MBID=%s (%s)", mbid, label)
        album_info = client.search_by_mbid(mbid)
        if not album_info:
            logger.warning("Lidarr: MBID=%s not found in catalog (%s)", mbid, label)
            return None

        foreign_album_id = album_info.get("foreignAlbumId", "")
        album_title = album_info.get("title", "?")

        existing = client.album_by_foreign_id(foreign_album_id) if foreign_album_id else None
        if existing and existing.get("id"):
            self._dispatch_existing(client, existing, mbid, track)

        logger.info("Lidarr: adding album '%s' (MBID=%s)", album_title, mbid)
        added = None
        try:
            added = client.add_album(album_info)
        except LidarrAlbumExists:
            # Aggiunto nel frattempo (un'altra traccia dello stesso album):
            # lo si cerca e si prosegue come per un album già presente.
            existing = client.album_by_foreign_id(foreign_album_id) if foreign_album_id else None
            if not (existing and existing.get("id")):
                raise RuntimeError(
                    f"Lidarr says album '{album_title}' already exists but does not list it yet"
                ) from None
            self._dispatch_existing(client, existing, mbid, track)
        if added:
            logger.info("Lidarr: SUCCESS — album '%s' (MBID=%s) queued", album_title, mbid)
            # L'id interno dell'album è l'handle per la riconciliazione (poll_delegation).
            handle = str(added.get("id")) if added.get("id") else None
            raise DownloadDelegated(
                provider=self.name,
                details=[{"mbid": mbid, "album": album_title, "status": "queued"}],
                handle=handle,
            )

        logger.error("Lidarr: FAILED to add album '%s' (MBID=%s)", album_title, mbid)
        return None

    def _dispatch_existing(self, client: _LidarrClient, album: dict, mbid: str, track: Track | None) -> None:
        """
        L'album è già in Lidarr: esserci non basta a delegare (prima era una
        delega finta — niente veniva cercato e la riconciliazione aspettava
        il max-age). Per una traccia richiesta Lidarr non decide: riporta cosa
        ha (``DelegateHoldsRelease``: le tracce con un file, se l'album è
        completo) e l'host stabilisce se la traccia c'è già (delega reale: il
        primo poll la trova), se l'album è completo senza di lei (rifiutata:
        ``already_in_delegate``) o se va cercata (``search_delegated``).
        Senza traccia (l'album intero) si cerca subito.

        Un Lidarr irraggiungibile o che rifiuta la ricerca solleva: per il
        resolver è un errore transitorio.
        """
        album_id = int(album["id"])
        album_title = album.get("title", "?")
        handle = str(album_id)

        if track is not None:
            lidarr_tracks = client.tracks_for_album(album_id)
            stats = album.get("statistics", {}) or {}
            track_count = stats.get("trackCount") or 0
            raise DelegateHoldsRelease(
                self.name, handle, _tracks_with_file(lidarr_tracks, album),
                complete=bool(track_count and (stats.get("trackFileCount") or 0) >= track_count),
                release=album_title,
                details=[{"mbid": mbid, "album": album_title, "status": "already_in_lidarr"}],
                state={"monitored": bool(album.get("monitored"))},
            )

        self._search_album(client, album_id, album_title, album.get("monitored"), mbid)

    def search_delegated(self, held: DelegateHoldsRelease, session: AuthSession) -> None:
        """Chiede a Lidarr di cercare l'album che l'host ha trovato senza la
        traccia (monitorandolo se non lo è): quella ricerca è la delega."""
        client = self._client(session)
        if client is None:
            raise RuntimeError("Lidarr URL not configured")
        base = held.details[0] if held.details else {}
        self._search_album(client, int(held.handle), held.release, held.state.get("monitored"), base.get("mbid"))

    def _search_album(self, client: _LidarrClient, album_id: int, album_title: str, monitored, mbid) -> None:
        if not monitored and client.monitor_album(album_id) is None:
            raise RuntimeError(f"Lidarr refused to monitor album '{album_title}'")
        if client.command("AlbumSearch", albumIds=[album_id]) is None:
            raise RuntimeError(f"Lidarr refused the search for album '{album_title}'")
        logger.info("Lidarr: album '%s' already in library, search triggered", album_title)
        raise DownloadDelegated(
            provider=self.name,
            details=[{"mbid": mbid, "album": album_title, "status": "search_triggered"}],
            handle=str(album_id),
        )

    def _client(self, session: AuthSession) -> _LidarrClient | None:
        url = session.extra.get("url", "").strip()
        if not url:
            return None
        api_key = session.params.get("apikey", "")
        return _LidarrClient(url, api_key, self.context.http_timeout)


def _q_album_id(record: dict) -> int | None:
    """Estrae l'albumId da un record di coda Lidarr (campo diretto o annidato)."""
    if record.get("albumId") is not None:
        return _to_int(record.get("albumId")) or None
    album = record.get("album") or {}
    return _to_int(album.get("id")) or None


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _media_value(media: dict, *keys: str):
    """
    Legge il primo valore presente tra le possibili varianti di casing di una
    chiave ``mediaInfo`` di Lidarr (es. la R maiuscola di ``audioBitRate``
    contro il refuso storico ``audioBitrate`` con la r minuscola, che Lidarr
    non manda mai davvero — ogni file finiva con bitrate/sample-rate 0).
    Vive qui, non in core/: il nome di questi campi è vocabolario specifico
    dell'API Lidarr, non un concetto generico.
    """
    for key in keys:
        if key in media:
            return media[key]
    return None


def _tracks_with_file(lidarr_tracks: list[dict], album: dict) -> list[Track]:
    """
    Le tracce Lidarr che hanno GIÀ un file su disco, come Track di dominio:
    titolo e durata dalla risorsa ``/track``, artista dell'ALBUM (la risorsa non lo
    espone), album vuoto (il rilascio delegato, vedi ``list_delegated_tracks``).
    Se la traccia richiesta sia fra queste lo decide l'host.
    """
    artist_info = album.get("artist", {}) or {}
    album_artist = artist_info.get("artistName", "") or ""

    return [
        Track(
            title=t.get("title", "") or "",
            artist=album_artist,
            album="",
            duration_ms=_to_int(t.get("duration")),
        )
        for t in lidarr_tracks
        if t.get("hasFile") or t.get("trackFileId")
    ]
