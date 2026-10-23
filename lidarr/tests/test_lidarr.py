"""
The Lidarr provider, with the HTTP API of Lidarr (and MusicBrainz) simulated.

Ported from the main repository, where the same facts were asserted through the host (the resolver, the
delegation service, the webhook route). The host decisions stay there; here every assertion is on what the PROVIDER
does and says:

- ``test_lidarr_unreachable_and_conflict.py`` -> "Dispatch": unreachable is transient (it raises, it never answers
  "I don't have it"), "already exists" (409) is not a failure;
- ``test_lidarr_already_in_lidarr.py`` -> "An album already in Lidarr": the provider reports what Lidarr holds
  (``DelegateHoldsRelease``) and, asked to, searches (``search_delegated``). The host's choice between delegating,
  declining and searching (``core.download_resolver.download_fuzzy``) is not tested here;
- ``test_lidarr_reconciliation.py`` -> "Reconciliation": ``list_delegated_tracks``, ``poll_delegation``,
  ``list_delegated_files``. The per-track judgement (``services.delegation.poll_for_requested_track``) and the
  end-to-end webhook are the host's: here the provider's data it judges from are asserted;
- ``test_download_provider_transient_errors.py`` (its Lidarr half) -> "MusicBrainz lookup".

"Added here" has what the main repository did not test directly.
"""
from __future__ import annotations

import httpx
import pytest

import musizerr_provider_lidarr as lidarr_module
from helpers import RemoteFailure, make_track, via_runner
from musizerr_provider_lidarr import LidarrDownloadProvider, LidarrNotConfigured, LidarrRejected, LidarrUnreachable
from musizerr_sdk import delegation as ds
from musizerr_sdk.auth import AuthFlow, AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.errors import DelegateHoldsRelease, DownloadDelegated
from musizerr_sdk.manifest import build
from musizerr_sdk.models import Album


@pytest.fixture
def session():
    return AuthSession(flow=AuthFlow.API_KEY, params={"apikey": "test-key"}, extra={"url": "http://fake-lidarr"})


def _provider() -> LidarrDownloadProvider:
    return LidarrDownloadProvider(StaticProviderContext(provider_name="lidarr"))


# ======================================================================
# Dispatch: unreachable is transient, "already exists" is not a failure
# ======================================================================

ALBUM_ID = 812
TORNADO = make_track(title="Tornado", artist="Some Artist", album="Weather", duration_ms=200_000)


class _Lidarr:
    def __init__(self, *, unreachable: set[str] = frozenset(), post_album_status: int = 201,
                 listed_after_conflict: bool = True, post_album_body: dict | None = None) -> None:
        self.unreachable = set(unreachable)
        self.post_album_body = post_album_body
        self.post_album_status = post_album_status
        self.listed_after_conflict = listed_after_conflict
        self.album_added = False
        self.album = {"id": ALBUM_ID, "title": "Weather", "foreignAlbumId": "rg-w", "monitored": True,
                      "artist": {"artistName": "Some Artist"},
                      "statistics": {"trackCount": 2, "trackFileCount": 0}}
        self.posts: list[str] = []

    @staticmethod
    def _path(url: str) -> str:
        return url.split("/api/v1", 1)[1]

    def get(self, url, params=None, headers=None, timeout=None):
        path = self._path(url)
        if path in self.unreachable:
            raise httpx.ConnectError("timed out", request=httpx.Request("GET", url))
        listed = self.album_added and self.listed_after_conflict
        data = {
            "/search": [{"album": {"foreignAlbumId": "rg-w", "title": "Weather",
                                   "artist": {"artistName": "Some Artist"}}}],
            "/album": [self.album] if listed else [],
            "/track": [{"title": "Tornado", "duration": 200_000, "hasFile": False}],
            "/qualityProfile": [{"id": 1}], "/metadataProfile": [{"id": 1}],
            "/rootfolder": [{"path": "/music"}],
        }.get(path, [])
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    def post(self, url, headers=None, json=None, timeout=None):
        path = self._path(url)
        self.posts.append(path)
        if path in self.unreachable:
            raise httpx.ConnectError("timed out", request=httpx.Request("POST", url))
        if path == "/album":
            # 409: another dispatch added it a moment ago.
            self.album_added = True
            return httpx.Response(self.post_album_status, json=self.post_album_body or {"message": "This album has already been added."},
                                  request=httpx.Request("POST", url))
        return httpx.Response(201, json={"id": 1}, request=httpx.Request("POST", url))


@pytest.fixture
def install(monkeypatch):
    def _install(**kwargs) -> _Lidarr:
        fake = _Lidarr(**kwargs)
        monkeypatch.setattr(lidarr_module.httpx, "get", fake.get)
        monkeypatch.setattr(lidarr_module.httpx, "post", fake.post)
        monkeypatch.setattr(lidarr_module, "_find_release_group_mbid_for_track", lambda *a, **k: "rg-w")
        return fake
    return _install


@pytest.mark.parametrize("path", ["/search", "/album", "/qualityProfile"])
def test_an_unreachable_lidarr_at_dispatch_raises_instead_of_saying_not_found(install, session, path):
    install(unreachable={path})

    with pytest.raises(LidarrUnreachable):
        _provider().download_fuzzy(TORNADO, "/unused", session)


def test_the_host_hears_the_unreachable_lidarr_by_the_name_of_the_exception(install, session):
    """Through the runner the exception is not the provider's class any more: the host reads its name (and retries)."""
    install(unreachable={"/search"})

    with pytest.raises(RemoteFailure) as failed:
        via_runner(LidarrDownloadProvider, "download_fuzzy", TORNADO, "/unused", session, provider_name="lidarr")

    assert failed.value.type_name == LidarrUnreachable.__name__


def test_an_album_already_added_409_is_found_again_as_an_album_already_there(install, session):
    fake = install(post_album_status=409)

    with pytest.raises(DelegateHoldsRelease) as held:
        _provider().download_fuzzy(TORNADO, "/unused", session)
    assert held.value.handle == str(ALBUM_ID) and held.value.details[0]["status"] == "already_in_lidarr"

    # What the host does for a delegate that holds the release without the track: ask it to search.
    with pytest.raises(DownloadDelegated) as delegated:
        _provider().search_delegated(held.value, session)
    assert delegated.value.handle == str(ALBUM_ID)
    assert delegated.value.details[0]["status"] == "search_triggered"
    assert fake.posts == ["/album", "/command"]


def test_a_409_whose_album_is_not_listed_yet_is_an_error_to_retry(install, session):
    install(post_album_status=409, listed_after_conflict=False)

    with pytest.raises(RuntimeError, match="does not list it yet"):
        _provider().download_fuzzy(TORNADO, "/unused", session)


def test_a_refused_add_still_means_not_added(install, session):
    install(post_album_status=400)

    assert _provider().download_fuzzy(TORNADO, "/unused", session) is None


def test_a_new_album_is_delegated_with_the_lidarr_id_as_the_handle(install, session):
    install(post_album_body={"id": 77, "title": "Weather"})

    with pytest.raises(DownloadDelegated) as delegated:
        _provider().download_fuzzy(TORNADO, "/unused", session)

    assert delegated.value.handle == "77" and delegated.value.provider == "lidarr"
    assert delegated.value.details == [{"mbid": "rg-w", "album": "Weather", "status": "queued"}]


def test_a_delegation_travels_through_the_runner_as_the_contracts_own_exception(install, session, monkeypatch):
    install(post_album_body={"id": 77})
    monkeypatch.setattr(lidarr_module, "_find_release_group_mbid_for_album", lambda *a, **k: "rg-w")

    with pytest.raises(DownloadDelegated) as delegated:
        via_runner(LidarrDownloadProvider, "dispatch_album", Album(title="Weather", artist="Some Artist"), session,
                   provider_name="lidarr")

    assert delegated.value.handle == "77"


def test_an_album_whose_release_group_musicbrainz_does_not_know_is_a_value_error(monkeypatch, session):
    monkeypatch.setattr(lidarr_module, "_find_release_group_mbid_for_album", lambda *a, **k: None)

    with pytest.raises(ValueError, match="MBID not found"):
        _provider().dispatch_album(Album(title="Weather", artist="Some Artist"), session)


# ======================================================================
# An album already in Lidarr
# ======================================================================

HELD_ALBUM_ID = 459


class _FakeLidarr:
    """The HTTP API of Lidarr on the paths ``_LidarrClient`` uses, recording writes."""

    def __init__(self, *, monitored: bool, tracks: list[dict], command_status: int = 201) -> None:
        self.album = {
            "id": HELD_ALBUM_ID, "title": "Aerosmith", "foreignAlbumId": "rg-1", "monitored": monitored,
            "artist": {"artistName": "Aerosmith"},
            "statistics": {"trackCount": len(tracks), "trackFileCount": sum(1 for t in tracks if t["hasFile"])},
        }
        self.tracks = tracks
        self.command_status = command_status
        self.posts: list[tuple[str, dict]] = []
        self.puts: list[tuple[str, dict]] = []

    @staticmethod
    def _path(url: str) -> str:
        return url.split("/api/v1", 1)[1]

    def get(self, url, params=None, headers=None, timeout=None):
        path = self._path(url)
        data = {
            "/search": [{"album": {"foreignAlbumId": "rg-1", "title": "Aerosmith", "artist": {"artistName": "Aerosmith"}}}],
            "/album": [self.album],
            f"/album/{HELD_ALBUM_ID}": self.album,
            "/track": self.tracks,
            "/queue": {"records": []},
            "/history/album": [],
        }.get(path, [])
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((self._path(url), json))
        return httpx.Response(self.command_status, json={"id": 1, "name": (json or {}).get("name")},
                              request=httpx.Request("POST", url))

    def put(self, url, headers=None, json=None, timeout=None):
        self.puts.append((self._path(url), json))
        return httpx.Response(202, json=[self.album], request=httpx.Request("PUT", url))


def _track(title: str, has_file: bool) -> dict:
    return {"title": title, "duration": 268_000, "hasFile": has_file, "trackFileId": 1 if has_file else None}


@pytest.fixture
def lidarr(monkeypatch):
    def install(**kwargs) -> _FakeLidarr:
        fake = _FakeLidarr(**kwargs)
        monkeypatch.setattr(lidarr_module.httpx, "get", fake.get)
        monkeypatch.setattr(lidarr_module.httpx, "post", fake.post)
        monkeypatch.setattr(lidarr_module.httpx, "put", fake.put)
        monkeypatch.setattr(lidarr_module, "_find_release_group_mbid_for_track", lambda *a, **k: "rg-1")
        return fake
    return install


REQUESTED = make_track(title="Dream On", artist="Aerosmith", album="Aerosmith", duration_ms=268_000)


def test_an_album_already_in_lidarr_without_the_track_is_reported_and_then_searched(lidarr, session):
    fake = lidarr(monitored=False, tracks=[_track("Dream On", False), _track("Mama Kin", True)])

    with pytest.raises(DelegateHoldsRelease) as held:
        _provider().download_fuzzy(REQUESTED, "/unused", session)

    # The provider reports what Lidarr holds, as data, and decides nothing.
    assert held.value.handle == str(HELD_ALBUM_ID) and held.value.release == "Aerosmith"
    assert [t.title for t in held.value.tracks] == ["Mama Kin"] and held.value.complete is False
    assert held.value.state == {"monitored": False}
    assert fake.posts == [] and fake.puts == []

    with pytest.raises(DownloadDelegated) as delegated:
        _provider().search_delegated(held.value, session)

    assert delegated.value.handle == str(HELD_ALBUM_ID)
    assert delegated.value.details[0]["status"] == "search_triggered"
    assert fake.puts == [("/album/monitor", {"albumIds": [HELD_ALBUM_ID], "monitored": True})]
    assert fake.posts == [("/command", {"name": "AlbumSearch", "albumIds": [HELD_ALBUM_ID]})]


def test_a_monitored_album_is_searched_without_touching_its_monitoring(lidarr, session):
    fake = lidarr(monitored=True, tracks=[_track("Dream On", False)])

    with pytest.raises(DelegateHoldsRelease) as held:
        _provider().download_fuzzy(REQUESTED, "/unused", session)
    with pytest.raises(DownloadDelegated):
        _provider().search_delegated(held.value, session)

    assert fake.puts == []
    assert fake.posts == [("/command", {"name": "AlbumSearch", "albumIds": [HELD_ALBUM_ID]})]


def test_a_track_whose_file_lidarr_already_has_is_reported_and_lands_at_the_first_poll(lidarr, session):
    fake = lidarr(monitored=True, tracks=[_track("Dream On", True)])

    with pytest.raises(DelegateHoldsRelease) as held:
        _provider().download_fuzzy(REQUESTED, "/unused", session)

    assert [t.title for t in held.value.tracks] == ["Dream On"] and held.value.complete is True
    assert held.value.details[0]["status"] == "already_in_lidarr"
    assert fake.posts == []  # nothing to search
    # What the host then polls: the album is fully imported and nothing is queued.
    assert _provider().poll_delegation(held.value.handle, session).state == ds.DOWNLOADED


def test_a_complete_album_without_the_track_is_reported_complete(lidarr, session):
    fake = lidarr(monitored=True, tracks=[_track("Mama Kin", True), _track("Seasons of Wither", True)])

    with pytest.raises(DelegateHoldsRelease) as held:
        _provider().download_fuzzy(REQUESTED, "/unused", session)

    assert held.value.complete is True
    assert "Dream On" not in [t.title for t in held.value.tracks]
    assert fake.posts == [] and fake.puts == []


def test_a_refused_search_is_an_error_not_a_delegation(lidarr, session):
    lidarr(monitored=True, tracks=[_track("Dream On", False)], command_status=500)

    with pytest.raises(DelegateHoldsRelease) as held:
        _provider().download_fuzzy(REQUESTED, "/unused", session)
    with pytest.raises(RuntimeError, match="refused the search"):
        _provider().search_delegated(held.value, session)


# ======================================================================
# Reconciliation
# ======================================================================


class _FakeLidarrHTTP:
    def __init__(self) -> None:
        self.reachable = True
        self._handlers: dict[str, object] = {}

    def set(self, path: str, data) -> None:
        self._handlers[path] = data

    def get(self, url, params=None, headers=None, timeout=None):
        # Guard against a regression: the key travels in the header, never in the URL, where it would end up in the
        # error messages and so in the logs.
        assert "apikey" not in (params or {}), "the API key must not be in the URL"
        assert (headers or {}).get("X-Api-Key"), "the API key must be in the X-Api-Key header"
        if not self.reachable:
            raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))
        path = url.split("/api/v1", 1)[1]
        handler = self._handlers.get(path)
        data = handler(params) if callable(handler) else (handler if handler is not None else [])
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))


@pytest.fixture
def fake_lidarr(monkeypatch):
    fake = _FakeLidarrHTTP()
    monkeypatch.setattr("musizerr_provider_lidarr.httpx.get", fake.get)
    return fake


def _album_payload(*, title="Album X", artist="Artist X", track_count=10, file_count=3):
    return {
        "title": title,
        "artist": {"artistName": artist},
        "statistics": {"trackCount": track_count, "trackFileCount": file_count},
    }


def _tracks_payload(entries: list[tuple[str, int, bool]]):
    """entries: (title, duration_ms, has_file)."""
    return [
        {"title": t, "duration": d, "hasFile": f, "trackFileId": (100 + i) if f else None}
        for i, (t, d, f) in enumerate(entries)
    ]


def test_the_requested_track_has_a_file_in_an_incomplete_album_and_the_provider_says_so(fake_lidarr, session):
    """The judgement per TRACK (not per album) is the host's; the provider gives it the data: the album is not
    complete (a whole-album poll is not "downloaded") and the tracks with a file are listed."""
    fake_lidarr.set("/album/123", _album_payload(track_count=10, file_count=3))
    fake_lidarr.set("/track", _tracks_payload([("Requested Song", 200_000, True), ("Other Song", 180_000, False)]))
    fake_lidarr.set("/queue", {"records": []})
    fake_lidarr.set("/history/album", [])

    tracks = _provider().list_delegated_tracks("123", session)

    assert [(t.title, t.duration_ms) for t in tracks] == [("Requested Song", 200_000)]
    assert _provider().poll_delegation("123", session).state != ds.DOWNLOADED


def test_a_track_without_a_file_is_never_listed_as_downloaded_just_for_being_in_the_album(fake_lidarr, session):
    fake_lidarr.set("/album/123", _album_payload(track_count=10, file_count=3))
    fake_lidarr.set("/track", _tracks_payload([("Requested Song", 200_000, False)]))

    assert _provider().list_delegated_tracks("123", session) == []


def test_list_delegated_tracks_gives_the_tracks_with_a_file_as_data_without_deciding(fake_lidarr, session):
    """The provider lists; it does not know (and does not say) which track was the requested one. The artist is the
    album's, the album stays empty: the host fills it."""
    fake_lidarr.set("/album/123", _album_payload(title="Album X (Deluxe)", artist="Artist X"))
    fake_lidarr.set("/track", _tracks_payload([
        ("Requested Song", 241_000, True),
        ("Other Song", 180_000, False),
        ("Third Song", 99_000, True),
    ]))

    tracks = _provider().list_delegated_tracks("123", session)

    assert [(t.title, t.artist, t.album, t.duration_ms) for t in tracks] == [
        ("Requested Song", "Artist X", "", 241_000),
        ("Third Song", "Artist X", "", 99_000),
    ]


def test_list_delegated_tracks_is_empty_for_a_handle_that_is_not_an_album(fake_lidarr, session):
    assert _provider().list_delegated_tracks("not-an-id", session) == []


def test_list_delegated_tracks_raises_lidarr_unreachable_on_connection_error(fake_lidarr, session):
    fake_lidarr.reachable = False

    with pytest.raises(LidarrUnreachable):
        _provider().list_delegated_tracks("123", session)


def test_poll_delegation_never_invents_downloading_when_no_signal(fake_lidarr, session):
    fake_lidarr.set("/album/123", _album_payload(track_count=10, file_count=0))
    fake_lidarr.set("/track", _tracks_payload([("Requested Song", 200_000, False)]))
    fake_lidarr.set("/queue", {"records": []})
    fake_lidarr.set("/history/album", [])

    status = _provider().poll_delegation("123", session)

    # No queue, no file: a real answer, not ignorance. But NEVER an invented "downloading", and not terminal by
    # itself (the host's max age decides if and when it becomes failed).
    assert status.state == ds.DISPATCHED
    assert status.state not in (ds.DOWNLOADING, ds.DOWNLOADED, ds.FAILED)


def test_poll_delegation_reports_downloading_when_queue_item_genuinely_active(fake_lidarr, session):
    fake_lidarr.set("/album/123", _album_payload(track_count=10, file_count=0))
    fake_lidarr.set("/track", _tracks_payload([("Requested Song", 200_000, False)]))
    fake_lidarr.set("/queue", {"records": [
        {"albumId": 123, "trackedDownloadState": "downloading", "trackedDownloadStatus": "ok"},
    ]})
    fake_lidarr.set("/history/album", [])

    assert _provider().poll_delegation("123", session).state == ds.DOWNLOADING


def test_poll_delegation_raises_lidarr_unreachable_on_connection_error(fake_lidarr, session):
    fake_lidarr.reachable = False

    with pytest.raises(LidarrUnreachable):
        _provider().poll_delegation("123", session)


def test_poll_delegation_judges_the_whole_album(fake_lidarr, session):
    """No track in the call: the historical behaviour at the level of the whole album."""
    fake_lidarr.set("/album/123", _album_payload(track_count=2, file_count=2))
    fake_lidarr.set("/queue", {"records": []})
    fake_lidarr.set("/history/album", [])

    assert _provider().poll_delegation("123", session).state == ds.DOWNLOADED


def test_a_failed_download_in_the_history_is_failed(fake_lidarr, session):
    fake_lidarr.set("/album/123", _album_payload(track_count=2, file_count=0))
    fake_lidarr.set("/history/album", [{"eventType": "downloadFailed"}])

    assert _provider().poll_delegation("123", session).state == ds.FAILED


def test_a_warning_about_files_that_already_exist_is_a_conflict(fake_lidarr, session):
    fake_lidarr.set("/album/123", _album_payload(track_count=2, file_count=0))
    fake_lidarr.set("/history/album", [])
    fake_lidarr.set("/queue", {"records": [{
        "albumId": 123, "trackedDownloadState": "importPending", "trackedDownloadStatus": "warning",
        "statusMessages": [{"title": "Import failed", "messages": ["Destination already exists"]}],
    }]})

    assert _provider().poll_delegation("123", session).state == ds.CONFLICT


@pytest.mark.parametrize(
    "media, expected_bitrate, expected_sample_rate",
    [
        # The real shape: audioBitRate (capital R) with the unit "kbps", audioSampleRate with the unit "kHz".
        ({"audioBitRate": "320 kbps", "audioSampleRate": "44.1kHz"}, 320_000, 44_100),
        ({"audioBitRate": "1411kbps", "audioSampleRate": "44100Hz"}, 1_411_000, 44_100),
        # The old wrong casing ("audioBitrate", small r) is tolerated.
        ({"audioBitrate": "128 kbps", "audioSamplerate": "22.05kHz"}, 128_000, 22_050),
        # Already a number (bit/s and Hz): passed through.
        ({"audioBitRate": 256_000, "audioSampleRate": 48_000}, 256_000, 48_000),
        # Unreadable or absent: unknown (0), never invented.
        ({"audioBitRate": "", "audioSampleRate": ""}, 0, 0),
        ({}, 0, 0),
    ],
)
def test_list_delegated_files_parses_real_media_info_shape(fake_lidarr, session, media, expected_bitrate, expected_sample_rate):
    fake_lidarr.set("/trackFile", [
        {
            "id": 7,
            "path": "/music/Artist/Album/01 Track.ogg",
            "mediaInfo": {"audioChannels": 2, "audioCodec": "OGG", "audioBits": "", **media},
            "quality": {"quality": {"name": "OGG"}},
        },
    ])

    refs = _provider().list_delegated_files("123", session)

    assert len(refs) == 1
    assert refs[0].bitrate == expected_bitrate
    assert refs[0].sample_rate == expected_sample_rate
    assert refs[0].external_id == "7" and refs[0].path.endswith("01 Track.ogg")


def test_a_flac_file_is_lossless_by_its_codec_or_its_quality_name(fake_lidarr, session):
    fake_lidarr.set("/trackFile", [
        {"id": 1, "path": "a.flac", "mediaInfo": {"audioCodec": "FLAC"}, "quality": {"quality": {"name": "FLAC"}}},
        {"id": 2, "path": "b.m4a", "mediaInfo": {"audioCodec": "AAC"}, "quality": {"quality": {"name": "ALAC"}}},
        {"id": 3, "path": "c.mp3", "mediaInfo": {"audioCodec": "MP3"}, "quality": {"quality": {"name": "MP3-320"}}},
    ])

    assert [ref.quality_lossless for ref in _provider().list_delegated_files("123", session)] == [True, True, False]


# ======================================================================
# MusicBrainz lookup, before the dispatch
# ======================================================================


def _musicbrainz(monkeypatch, response):
    def get(url, params=None, headers=None, timeout=None):
        if isinstance(response, Exception):
            raise response
        return httpx.Response(response, json={"recordings": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(lidarr_module.httpx, "get", get)


@pytest.mark.parametrize("response", [503, 429, httpx.ConnectError("refused")])
def test_lidarr_raises_when_musicbrainz_is_unavailable(monkeypatch, session, response):
    _musicbrainz(monkeypatch, response)

    with pytest.raises(lidarr_module.MusicBrainzUnavailable):
        _provider().download_fuzzy(make_track(title="Control"), "/unused", session)


def test_lidarr_answers_none_when_musicbrainz_does_not_know_the_track(monkeypatch, session):
    _musicbrainz(monkeypatch, 200)

    assert _provider().download_fuzzy(make_track(title="Control"), "/unused", session) is None


def test_the_release_group_is_found_through_the_recording_search(monkeypatch):
    seen = []

    def get(url, params=None, headers=None, timeout=None):
        seen.append((url, params, headers))
        body = {"recordings": [{"releases": [{"release-group": {"id": "rg-42"}}]}]}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(lidarr_module.httpx, "get", get)

    mbid = lidarr_module._find_release_group_mbid_for_track("Tornado", "Some Artist", "Weather", timeout=5)

    assert mbid == "rg-42"
    url, params, headers = seen[0]
    assert url == "https://musicbrainz.org/ws/2/recording" and params["fmt"] == "json"
    assert 'recording:"Tornado"' in params["query"] and 'release:"Weather"' in params["query"]
    assert headers["User-Agent"].lower().startswith("musizerr/")


# ======================================================================
# Added here
# ======================================================================


def test_the_provider_without_a_url_does_nothing(session):
    bare = AuthSession(flow=AuthFlow.API_KEY, params={"apikey": "k"}, extra={})
    provider = _provider()

    assert provider.test_connection(bare) == "Lidarr URL not configured"
    assert provider.poll_delegation("1", bare) is None
    assert provider.list_delegated_tracks("1", bare) == []
    assert provider.list_delegated_files("1", bare) == []
    assert provider.request_rescan(bare) is None
    with pytest.raises(LidarrNotConfigured, match="URL not configured"):  # a missing URL is a reason, not a silent None
        provider.register_push_endpoint("http://musizerr/hook", bare)
    assert provider.validate_config({}) is False and provider.validate_config({"url": "http://lidarr:8686"}) is True


def test_the_connection_test_reports_the_version_or_why_not(fake_lidarr, session):
    fake_lidarr.set("/system/status", {"version": "2.5.3"})
    assert _provider().test_connection(session) is None

    fake_lidarr.reachable = False
    assert "unreachable" in _provider().test_connection(session)


@pytest.mark.parametrize("payload, handle", [
    ({"eventType": "Download", "album": {"id": 12}}, "12"),
    ({"eventType": "TrackFileImported", "albumId": 7}, "7"),
    ({"eventType": "Download", "album": {"id": "12"}}, None),  # an id that is not a number is not an id
    ({"eventType": "Test", "album": {"id": 12}}, None),        # an event that says nothing about an album
    ({"eventType": "Download"}, None),
    ("Download", None),
])
def test_a_notification_of_lidarr_gives_the_album_handle_it_is_about(payload, handle):
    assert _provider().delegation_event_handle(payload) == handle


class _Notifications:
    def __init__(self, existing: list[dict]):
        self.existing = existing
        self.writes: list[tuple[str, str, dict]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        return httpx.Response(200, json=self.existing, request=httpx.Request("GET", url))

    def post(self, url, headers=None, json=None, timeout=None):
        self.writes.append(("POST", url.split("/api/v1", 1)[1], json))
        return httpx.Response(201, json={"id": 9, **json}, request=httpx.Request("POST", url))

    def put(self, url, headers=None, json=None, timeout=None):
        self.writes.append(("PUT", url.split("/api/v1", 1)[1], json))
        return httpx.Response(202, json=json, request=httpx.Request("PUT", url))


def _notifications(monkeypatch, existing: list[dict]) -> _Notifications:
    fake = _Notifications(existing)
    monkeypatch.setattr(lidarr_module.httpx, "get", fake.get)
    monkeypatch.setattr(lidarr_module.httpx, "post", fake.post)
    monkeypatch.setattr(lidarr_module.httpx, "put", fake.put)
    return fake


def test_registering_the_webhook_creates_the_connection_once_and_updates_it_after(monkeypatch, session):
    fake = _notifications(monkeypatch, [{"id": 3, "name": "someone else's"}])
    assert _provider().register_push_endpoint("http://musizerr/hook", session) == "created"
    method, path, body = fake.writes[0]
    assert (method, path, body["name"], body["implementation"]) == ("POST", "/notification", "musizerr", "Webhook")
    assert {"name": "url", "value": "http://musizerr/hook"} in body["fields"]

    fake = _notifications(monkeypatch, [{"id": 3, "name": "musizerr"}])
    assert _provider().register_push_endpoint("http://musizerr/new-hook", session) == "updated"
    method, path, body = fake.writes[0]
    assert (method, path, body["id"]) == ("PUT", "/notification/3", 3)


def test_a_file_is_deleted_by_its_lidarr_id(monkeypatch, session):
    deleted = []

    def delete(url, headers=None, timeout=None):
        deleted.append((url, headers))
        return httpx.Response(200, json={}, request=httpx.Request("DELETE", url))

    monkeypatch.setattr(lidarr_module.httpx, "delete", delete)
    from musizerr_sdk.delegation import FileRef

    assert _provider().delete_file(FileRef(path="/m/a.mp3", external_id="7"), session) is True
    assert deleted == [("http://fake-lidarr/api/v1/trackFile/7", {"X-Api-Key": "test-key"})]
    assert _provider().delete_file(FileRef(path="/m/a.mp3"), session) is False  # no id: nothing to delete


def test_the_manifest_declares_a_fuzzy_delegating_download_provider_that_is_off_by_default():
    manifest = build(_provider())

    assert (manifest.name, manifest.label, manifest.roles) == ("lidarr", "Lidarr", ("download",))
    assert manifest.declarations["supports_fuzzy"] is True
    assert manifest.declarations["enabled_by_default"] is False
    assert manifest.declarations["supported_identifier_types"] == set() or not manifest.declarations["supported_identifier_types"]
    for method in ("download_fuzzy", "dispatch_album", "poll_delegation", "search_delegated", "list_delegated_tracks",
                   "list_delegated_files", "delete_file", "request_rescan", "register_push_endpoint",
                   "delegation_event_handle"):
        assert method in manifest.implements


# ======================================================================
# Added here: what the sandbox changes (the one change made to the code in the move)
# ======================================================================


class _Status:
    """A Lidarr whose every answer is ``status``, except the paths in ``ok`` (a normal answer). In the sandbox a plain-HTTP
    request to an address that does not respond comes back as a 502 from the gateway's egress proxy, not as a connection
    error."""

    def __init__(self, status: int, ok: dict[str, object] | None = None) -> None:
        self.status, self.ok = status, ok or {}

    def _reply(self, method: str, url: str, **kw):
        path = url.split("/api/v1", 1)[1]
        if method == "GET" and path in self.ok:
            return httpx.Response(200, json=self.ok[path], request=httpx.Request(method, url))
        return httpx.Response(self.status, text="cannot connect to lidarr.lan:8686", request=httpx.Request(method, url))

    def get(self, url, **kw):
        return self._reply("GET", url)

    def post(self, url, **kw):
        return self._reply("POST", url)

    def put(self, url, **kw):
        return self._reply("PUT", url)


def _answering(monkeypatch, status: int, ok: dict | None = None) -> None:
    fake = _Status(status, ok)
    monkeypatch.setattr(lidarr_module.httpx, "get", fake.get)
    monkeypatch.setattr(lidarr_module.httpx, "post", fake.post)
    monkeypatch.setattr(lidarr_module.httpx, "put", fake.put)
    monkeypatch.setattr(lidarr_module, "_find_release_group_mbid_for_track", lambda *a, **k: "rg-w")


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_status_on_a_read_is_an_unreachable_lidarr_not_a_not_found(monkeypatch, session, status):
    _answering(monkeypatch, status)

    with pytest.raises(LidarrUnreachable):
        _provider().download_fuzzy(TORNADO, "/unused", session)           # the search
    with pytest.raises(LidarrUnreachable):
        _provider().poll_delegation("123", session)                       # the album
    assert "unreachable" in _provider().test_connection(session)


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_status_on_a_write_is_an_unreachable_lidarr(monkeypatch, session, status):
    search = [{"album": {"foreignAlbumId": "rg-w", "title": "Weather", "artist": {"artistName": "Some Artist"}}}]
    ok = {"/search": search, "/album": [], "/qualityProfile": [{"id": 1}], "/metadataProfile": [{"id": 1}], "/rootfolder": [{"path": "/m"}]}
    _answering(monkeypatch, status, ok)
    with pytest.raises(LidarrUnreachable):
        _provider().download_fuzzy(TORNADO, "/unused", session)           # POST /album

    held = DelegateHoldsRelease("lidarr", "5", [], complete=False, release="Weather", details=[], state={"monitored": False})
    _answering(monkeypatch, status, {"/album/5": {}})
    with pytest.raises(LidarrUnreachable):
        _provider().search_delegated(held, session)                       # PUT /album/monitor


@pytest.mark.parametrize("status", [400, 401, 404, 500])
def test_any_other_status_is_still_lidarrs_own_answer(monkeypatch, session, status):
    _answering(monkeypatch, status)

    assert _provider().download_fuzzy(TORNADO, "/unused", session) is None   # "not in the catalog": a definitive answer
    assert "No response from Lidarr" in _provider().test_connection(session)


def test_the_unreachable_lidarr_of_the_sandbox_reaches_the_host_by_the_name_of_the_exception(monkeypatch, session):
    _answering(monkeypatch, 502)

    with pytest.raises(RemoteFailure) as failed:
        via_runner(LidarrDownloadProvider, "poll_delegation", "123", session, provider_name="lidarr")

    assert failed.value.type_name == "LidarrUnreachable"


# ======================================================================
# Registering the push callback: a refusal says why, and never shows the token
# ======================================================================

CALLBACK = "http://frontend/api/delegation/webhook/lidarr?token=s3cr3t-callback-token"
TOKEN = "s3cr3t-callback-token"


class _Notification:
    """A Lidarr whose notification endpoints answer what they are given: ``get`` for the list, ``write`` for the
    POST/PUT of the connection (a status and a body, or an exception to raise)."""

    def __init__(self, *, listed=None, write=None, get=None) -> None:
        self.listed = [] if listed is None else listed
        self.write = write if write is not None else (201, {"id": 9})
        self.get_answer = get
        self.calls: list[tuple[str, str]] = []

    def _answer(self, method: str, url: str, status, body):
        self.calls.append((method, url.split("/api/v1", 1)[1]))
        if isinstance(status, Exception):
            raise status
        kwargs = {"json": body} if body is not None else {"text": "not json"}
        return httpx.Response(status, request=httpx.Request(method, url), **kwargs)

    def get(self, url, params=None, headers=None, timeout=None):
        status, body = self.get_answer if self.get_answer is not None else (200, self.listed)
        return self._answer("GET", url, status, body)

    def post(self, url, headers=None, json=None, timeout=None):
        return self._answer("POST", url, *self.write)

    def put(self, url, headers=None, json=None, timeout=None):
        return self._answer("PUT", url, *self.write)


def _lidarr_notifications(monkeypatch, **kwargs) -> _Notification:
    fake = _Notification(**kwargs)
    monkeypatch.setattr(lidarr_module.httpx, "get", fake.get)
    monkeypatch.setattr(lidarr_module.httpx, "post", fake.post)
    monkeypatch.setattr(lidarr_module.httpx, "put", fake.put)
    return fake


# What Lidarr answers (400) when it cannot reach the callback address it was given: a list of validation failures,
# each echoing what was sent in ``attemptedValue``.
VALIDATION = [
    {
        "propertyName": "Url", "errorMessage": "Unable to send test message", "attemptedValue": CALLBACK,
        "severity": "error", "errorCode": "NotNullValidator",
    },
    {"propertyName": "", "errorMessage": "Name or service not known (frontend:80)", "severity": "error"},
]


def test_a_validation_failure_of_lidarr_is_raised_with_its_reason(monkeypatch, session):
    fake = _lidarr_notifications(monkeypatch, write=(400, VALIDATION))

    with pytest.raises(LidarrRejected) as refused:
        _provider().register_push_endpoint(CALLBACK, session)

    assert str(refused.value) == (
        "Lidarr refused the request (400): Url: Unable to send test message; Name or service not known (frontend:80)"
    )
    assert refused.value.response.status_code == 400
    assert fake.calls == [("GET", "/notification"), ("POST", "/notification")]


def test_the_callback_token_is_not_in_the_message_even_when_lidarr_quotes_the_url(monkeypatch, session):
    quoting = [{"propertyName": "Url", "errorMessage": f"Unable to reach {CALLBACK} (token {TOKEN} was sent)", "attemptedValue": CALLBACK}]
    _lidarr_notifications(monkeypatch, write=(400, quoting))

    with pytest.raises(LidarrRejected) as refused:
        _provider().register_push_endpoint(CALLBACK, session)

    assert TOKEN not in str(refused.value)
    assert "token=***" in str(refused.value)


def test_the_token_is_not_logged_either(monkeypatch, session, caplog):
    quoting = [{"propertyName": "Url", "errorMessage": f"Unable to reach {CALLBACK}"}]
    _lidarr_notifications(monkeypatch, write=(400, quoting))

    with caplog.at_level("DEBUG"), pytest.raises(LidarrRejected):
        _provider().register_push_endpoint(CALLBACK, session)

    assert TOKEN not in caplog.text


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_api_key_is_said_and_echoes_nothing(monkeypatch, session, status):
    _lidarr_notifications(monkeypatch, write=(status, {"message": "Unauthorized", "attemptedValue": CALLBACK}))

    with pytest.raises(LidarrRejected) as refused:
        _provider().register_push_endpoint(CALLBACK, session)

    assert str(refused.value) == f"Lidarr refused the API key ({status})"


def test_an_error_of_lidarr_itself_is_raised_with_its_message(monkeypatch, session):
    _lidarr_notifications(monkeypatch, write=(500, {"message": "NullReferenceException", "description": "stack"}))

    with pytest.raises(LidarrRejected, match=r"\(500\): NullReferenceException$"):
        _provider().register_push_endpoint(CALLBACK, session)


def test_an_error_without_a_readable_body_says_the_status(monkeypatch, session):
    _lidarr_notifications(monkeypatch, write=(409, None))

    with pytest.raises(LidarrRejected, match=r"\(409\): Conflict"):
        _provider().register_push_endpoint(CALLBACK, session)


def test_a_refusal_when_updating_the_existing_connection_is_raised_too(monkeypatch, session):
    fake = _lidarr_notifications(monkeypatch, listed=[{"id": 3, "name": "musizerr"}], write=(400, VALIDATION))

    with pytest.raises(LidarrRejected, match="Unable to send test message"):
        _provider().register_push_endpoint(CALLBACK, session)

    assert fake.calls == [("GET", "/notification"), ("PUT", "/notification/3")]


def test_a_failed_listing_stops_the_registration_instead_of_creating_a_duplicate(monkeypatch, session):
    fake = _lidarr_notifications(monkeypatch, get=(401, {"message": "Unauthorized"}))

    with pytest.raises(LidarrRejected, match="refused the API key"):
        _provider().register_push_endpoint(CALLBACK, session)

    assert fake.calls == [("GET", "/notification")]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_status_is_an_unreachable_lidarr(monkeypatch, session, status):
    _lidarr_notifications(monkeypatch, get=(status, None))

    with pytest.raises(LidarrUnreachable, match="Lidarr unreachable"):
        _provider().register_push_endpoint(CALLBACK, session)


def test_a_lidarr_that_does_not_answer_is_unreachable_with_the_reason(monkeypatch, session):
    _lidarr_notifications(monkeypatch, get=(httpx.ConnectError("[Errno -2] Name or service not known"), None))

    with pytest.raises(LidarrUnreachable, match="Lidarr unreachable: .*Name or service not known"):
        _provider().register_push_endpoint(CALLBACK, session)


def test_an_accepted_connection_with_an_unreadable_body_is_still_created(monkeypatch, session):
    _lidarr_notifications(monkeypatch, write=(201, None))

    assert _provider().register_push_endpoint(CALLBACK, session) == "created"


@pytest.mark.parametrize("write, kind", [
    ((400, VALIDATION), "failed"),
    ((401, {"message": "Unauthorized"}), "auth_failed"),
    ((404, {"message": "NotFound"}), "not_found"),
    ((500, {"message": "boom"}), "upstream_error"),
])
def test_the_host_hears_a_refusal_with_its_kind_and_reason(monkeypatch, session, write, kind):
    """Through the runner: the failure is classified by the status of the response, and the message is Lidarr's."""
    _lidarr_notifications(monkeypatch, write=write)

    with pytest.raises(RemoteFailure) as failed:
        via_runner(LidarrDownloadProvider, "register_push_endpoint", CALLBACK, session, provider_name="lidarr")

    assert failed.value.kind == kind
    assert failed.value.type_name == "LidarrRejected"
    assert str(failed.value).startswith("Lidarr refused")
    assert TOKEN not in str(failed.value)


def test_the_host_hears_an_unreachable_lidarr_by_the_name_of_the_exception(monkeypatch, session):
    _lidarr_notifications(monkeypatch, get=(httpx.ConnectError("[Errno -3] Temporary failure in name resolution"), None))

    with pytest.raises(RemoteFailure) as failed:
        via_runner(LidarrDownloadProvider, "register_push_endpoint", CALLBACK, session, provider_name="lidarr")

    assert failed.value.type_name == "LidarrUnreachable"
    assert "name resolution" in str(failed.value)


def test_a_registration_that_worked_still_travels_as_its_outcome_word(monkeypatch, session):
    _lidarr_notifications(monkeypatch, write=(201, {"id": 9}))

    assert via_runner(LidarrDownloadProvider, "register_push_endpoint", CALLBACK, session, provider_name="lidarr") == "created"
