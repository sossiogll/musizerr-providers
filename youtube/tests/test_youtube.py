"""
The YouTube likes importer, with the HTTP answers simulated.

Ported from the main repository's ``test_youtube_provider.py``. What exercised the HOST with this provider stays
there (the OAuth authorize URL built by the host, the registry's discovery, the search fan-out, the error bodies);
here the same facts are asserted on the provider and on what its runner tells the host (``classify_exception``).

- a liked video becomes a track (artist and title cleaned, duration from the ISO
  string, best thumbnail), only for the Music category;
- ONE ``videos.list`` call per page (1 quota unit), following ``nextPageToken``,
  and a page the category filter left short or empty never ends the import;
- taking a like off needs the scope the connection may lack;
- an exhausted quota is "rate limited", never "no likes";
- it has no catalog: nothing that searches or looks up an album, an artist
  or a chart ever reaches the network.
"""
from __future__ import annotations

import httpx
import pytest

import musizerr_provider_youtube as youtube_module
from musizerr_sdk.auth import AuthSession
from musizerr_sdk.capabilities import CAPABILITY_UNLIKE_SAVED_TRACK, CAPABILITY_USER_LIBRARY
from musizerr_sdk.identifiers import PROVIDER_ID
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.errors import ProviderRateLimited
from musizerr_sdk.auth import BearerHeaderRequestAuth, RequestBodyClientAuth
from musizerr_provider_youtube import YouTubeMetadataProvider, _artist_and_title, _duration_ms
from musizerr_sdk.failures import classify_exception
from musizerr_sdk.manifest import build

from helpers import declared_capabilities, fetch_all_saved_tracks, make_track, via_runner

SESSION = AuthSession.oauth2(headers={"Authorization": "Bearer token"})
BASE = "https://www.googleapis.com/youtube/v3"


class FakeYouTube:
    """The API: ``pages`` of videos, linked by ``nextPageToken``. Every request
    is recorded."""

    def __init__(self, pages: list[list[dict]], *, tokens: bool = True):
        self.pages = pages
        self.tokens = tokens
        self.calls: list[tuple[str, str, dict]] = []
        self.answers: list[httpx.Response] = []  # scripted answers, served first
        self.override = None  # callable(url, params) -> Response | None, asked before the pages

    def response(self, status: int, body: dict | None, method: str, url: str) -> httpx.Response:
        return httpx.Response(status, json=body, request=httpx.Request(method, url)) if body is not None else \
            httpx.Response(status, request=httpx.Request(method, url))

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {})))
        assert headers == {"Authorization": "Bearer token"}
        if self.answers:
            return self.answers.pop(0)
        if self.override is not None and (answer := self.override(url, params or {})) is not None:
            return answer
        index = int((params or {}).get("pageToken", "p0")[1:])
        body = {"items": self.pages[index]}
        if self.tokens and index + 1 < len(self.pages):
            body["nextPageToken"] = f"p{index + 1}"
        return self.response(200, body, "GET", url)

    def post(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("POST", url, dict(params or {})))
        return self.answers.pop(0) if self.answers else self.response(204, None, "POST", url)


@pytest.fixture
def api(monkeypatch):
    def _install(pages=None, **kwargs):
        fake = FakeYouTube(pages or [[]], **kwargs)
        monkeypatch.setattr(youtube_module.httpx, "get", fake.get)
        monkeypatch.setattr(youtube_module.httpx, "post", fake.post)
        return fake

    return _install


def _provider() -> YouTubeMetadataProvider:
    return YouTubeMetadataProvider(StaticProviderContext(provider_name="youtube"))


def _video(n: int, *, category: str = "10", title: str | None = None, channel: str = "Band - Topic",
           duration: str = "PT3M30S", thumbnails: dict | None = None) -> dict:
    return {
        "id": f"vid{n}",
        "snippet": {
            "title": title or f"Song {n}", "channelTitle": channel, "categoryId": category,
            "publishedAt": "2020-01-01T00:00:00Z",
            "thumbnails": thumbnails or {"default": {"url": f"https://img/{n}/d.jpg", "width": 120, "height": 90}},
        },
        "contentDetails": {"duration": duration},
    }


def _error(reason: str, status: int = 403) -> dict:
    return {"error": {"code": status, "message": f"{reason} happened", "errors": [{"reason": reason}]}}


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def test_the_provider_is_declared_for_google_oauth_with_the_unlike_scope_as_a_capability():
    oauth2 = YouTubeMetadataProvider.auth.oauth2

    assert oauth2.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
    assert oauth2.token_url == "https://oauth2.googleapis.com/token"
    assert oauth2.scopes == ["https://www.googleapis.com/auth/youtube.readonly"]
    assert oauth2.capability_scopes == {CAPABILITY_UNLIKE_SAVED_TRACK: ["https://www.googleapis.com/auth/youtube"]}
    assert oauth2.requested_scopes() == [
        "https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/youtube",
    ]
    assert isinstance(oauth2.token_client_auth, RequestBodyClientAuth)
    assert isinstance(oauth2.request_auth, BearerHeaderRequestAuth)
    # The likes are the user's own: no app-only fallback.
    assert oauth2.fallback_auth is None
    assert oauth2.extra_authorize_params == {"access_type": "offline", "prompt": "consent"}


def test_the_provider_declares_the_library_and_the_unlike_capabilities_only():
    assert declared_capabilities(_provider()) == [CAPABILITY_USER_LIBRARY, CAPABILITY_UNLIKE_SAVED_TRACK]
    assert _provider().config_schema() == []


def test_the_manifest_describes_a_metadata_provider_with_the_library_and_unlike_capabilities():
    manifest = build(_provider())

    assert (manifest.name, manifest.label, manifest.roles) == ("youtube", "YouTube", ("metadata",))
    assert manifest.declarations["supports_user_library"] is True and manifest.declarations["supports_unlike_saved_track"] is True
    assert manifest.declarations["supports_discography"] is False and manifest.declarations["streams_full_tracks"] is False
    assert manifest.auth == YouTubeMetadataProvider.auth


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def test_a_liked_video_is_mapped_to_a_track(api):
    thumbs = {
        "default": {"url": "https://img/d.jpg", "width": 120, "height": 90},
        "high": {"url": "https://img/h.jpg", "width": 480, "height": 360},
        "maxres": {"url": "https://img/m.jpg", "width": 1280, "height": 720},
        "medium": {"url": "https://img/md.jpg", "width": 320, "height": 180},
    }
    api([[_video(1, title="Blank Space (Official Video)", channel="Taylor Swift - Topic", duration="PT3M51S",
                 thumbnails=thumbs)]])

    (track,) = _provider().get_saved_tracks(SESSION)

    assert (track.title, track.artist, track.album) == ("Blank Space", "Taylor Swift", "")
    assert track.provider == "youtube" and track.provider_id == "vid1"
    assert track.duration_ms == 231_000
    assert track.cover_url == "https://img/m.jpg"
    assert [(i.type, i.value, i.provider) for i in track.external_ids] == [(PROVIDER_ID, "vid1", "youtube")]
    # Never played or downloaded: no audio to point at; and no like time is given.
    assert track.preview_url is None and track.stream_url is None
    assert track.saved_at is None and track.year is None


def test_only_the_music_category_is_kept(api):
    api([[_video(1), _video(2, category="24"), _video(3, category="22"), _video(4)]])

    tracks = _provider().get_saved_tracks(SESSION)

    assert [t.provider_id for t in tracks] == ["vid1", "vid4"]


def test_a_video_without_thumbnails_or_duration_still_maps(api):
    video = _video(1)
    video["snippet"]["thumbnails"] = {}
    del video["contentDetails"]
    api([[video]])

    (track,) = _provider().get_saved_tracks(SESSION)

    assert track.cover_url is None and track.duration_ms == 0


@pytest.mark.parametrize("iso, ms", [
    ("PT4M13S", 253_000), ("PT1H2M3S", 3_723_000), ("PT45S", 45_000), ("PT1H", 3_600_000), ("PT2M", 120_000),
    ("P1DT1H", 90_000_000), ("PT3M0.5S", 180_500), ("P0D", 0), ("", 0), (None, 0), ("garbage", 0),
])
def test_iso_8601_durations(iso, ms):
    assert _duration_ms(iso) == ms


@pytest.mark.parametrize("video_title, channel, expected", [
    # The channel names the artist: "Name - Topic" carries the plain song title.
    ("Song", "Artist - Topic", ("Artist", "Song")),
    ("Song (Official Video)", "Artist - Topic", ("Artist", "Song")),
    ("Song", "Artist - topic", ("Artist", "Song")),
    # VEVO channels.
    ("Song (Official Video)", "ArtistVEVO", ("Artist", "Song")),
    ("Blank Space", "TaylorSwiftVEVO", ("Taylor Swift", "Blank Space")),
    ("Ariana Grande - 7 rings (Official Video)", "ArianaGrandeVevo", ("Ariana Grande", "7 rings")),
    ("Song", "ZAYN VEVO", ("ZAYN", "Song")),
    # "Artist - Title" on the artist's own channel and on someone else's.
    ("Daft Punk - One More Time (Official Video)", "Daft Punk", ("Daft Punk", "One More Time")),
    ("Some Artist - Some Song [HD]", "Random Uploads", ("Some Artist", "Some Song")),
    ("Some Artist – Some Song", "Random Uploads", ("Some Artist", "Some Song")),
    ("Artist - Song | Official Video", "Label", ("Artist", "Song")),
    ('Artist - "Song"', "Artist", ("Artist", "Song")),
    ("Max Pezzali - Con un deca (Video Ufficiale)", "Max Pezzali", ("Max Pezzali", "Con un deca")),
    # "Title - Artist", the channel being the artist.
    ("Song - Artist", "Artist", ("Artist", "Song")),
    # A real version tail stays in the title, whatever follows the first separator.
    ("Artist - Song - Live at Wembley", "Chan", ("Artist", "Song - Live at Wembley")),
    ("Artist - Song (Remix) [Official Video]", "Artist", ("Artist", "Song (Remix)")),
    # No separator: the channel is the artist.
    ("Just A Title", "Some Channel", ("Some Channel", "Just A Title")),
    # A left side that is no artist (a sentence) is not taken for one.
    ("this is a long sentence of nine words before the dash - rest", "Chan",
     ("Chan", "this is a long sentence of nine words before the dash - rest")),
])
def test_artist_and_title_of_a_video(video_title, channel, expected):
    assert _artist_and_title(video_title, channel) == expected


def test_a_title_that_is_only_noise_is_kept_as_it_is():
    assert _artist_and_title("(Official Video)", "Artist - Topic") == ("Artist", "(Official Video)")


# ---------------------------------------------------------------------------
# Pagination: one call per page
# ---------------------------------------------------------------------------


def _likes(count: int, *, per_page: int = 50, category: str = "10") -> list[list[dict]]:
    videos = [_video(n, category=category) for n in range(count)]
    return [videos[i:i + per_page] for i in range(0, count, per_page)]


def test_the_import_costs_one_call_per_page(api):
    fake = api(_likes(120))

    tracks = fetch_all_saved_tracks(_provider(), SESSION)

    assert len(tracks) == 120
    assert [t.provider_id for t in tracks] == [f"vid{n}" for n in range(120)]
    # 120 likes over 3 pages: 3 calls (3 quota units), never a walk from the start per page.
    assert len(fake.calls) == 3
    assert [c[2].get("pageToken") for c in fake.calls] == [None, "p1", "p2"]
    for method, url, params in fake.calls:
        assert (method, url) == ("GET", f"{BASE}/videos")
        assert params["myRating"] == "like" and params["maxResults"] == 50
        assert params["part"] == "snippet,contentDetails"


def test_the_host_reports_progress_once_per_page(api):
    api(_likes(120))
    seen: list[int] = []

    fetch_all_saved_tracks(_provider(), SESSION, on_page=seen.append)

    assert seen == [50, 100, 120]


def test_a_page_the_category_filter_left_short_does_not_end_the_import(api):
    """Page 1: 50 videos, 10 of them music; page 2: no music at all; page 3: the last, 5 music."""
    pages = [
        [_video(n, category="10" if n < 10 else "22") for n in range(50)],
        [_video(n, category="24") for n in range(50, 100)],
        [_video(n, category="10" if n < 105 else "22") for n in range(100, 110)],
    ]
    fake = api(pages)

    tracks = fetch_all_saved_tracks(_provider(), SESSION)

    assert [t.provider_id for t in tracks] == [f"vid{n}" for n in [*range(10), *range(100, 105)]]
    assert len(fake.calls) == 3


def test_the_last_page_is_the_one_without_a_next_page_token(api):
    fake = api(_likes(50))  # exactly one full page, and no token after it

    tracks = fetch_all_saved_tracks(_provider(), SESSION)

    assert len(tracks) == 50 and len(fake.calls) == 1


def test_an_empty_library_is_one_call_and_no_tracks(api):
    fake = api([[]])

    assert fetch_all_saved_tracks(_provider(), SESSION) == []
    assert len(fake.calls) == 1


def test_a_repeated_page_token_ends_the_walk(api):
    fake = api([[_video(1)]])
    fake.override = lambda url, params: fake.response(200, {"items": [_video(1)], "nextPageToken": "same"}, "GET", url)

    tracks = fetch_all_saved_tracks(_provider(), SESSION)

    assert len(tracks) == 2  # the first page and the one "same" led to, then the repeated token ends it
    assert len(fake.calls) == 2


def test_get_saved_tracks_reads_only_the_pages_it_needs(api):
    fake = api(_likes(120))

    first = _provider().get_saved_tracks(SESSION, limit=50, offset=0)
    assert [t.provider_id for t in first] == [f"vid{n}" for n in range(50)]
    assert len(fake.calls) == 1

    fake.calls.clear()
    second = _provider().get_saved_tracks(SESSION, limit=50, offset=50)
    assert [t.provider_id for t in second] == [f"vid{n}" for n in range(50, 100)]
    assert len(fake.calls) == 2


def test_a_page_size_above_the_api_maximum_is_capped(api):
    fake = api(_likes(3))

    list(_provider().iter_saved_tracks(SESSION, page_size=500))

    assert fake.calls[0][2]["maxResults"] == 50


# ---------------------------------------------------------------------------
# get_track
# ---------------------------------------------------------------------------


def test_get_track_is_one_call_by_id_without_the_category_filter(api):
    fake = api([[_video(7, category="22", title="Artist - Song (Lyrics)", channel="Artist")]])

    track = _provider().get_track("vid7", SESSION)

    assert (track.artist, track.title, track.provider_id) == ("Artist", "Song", "vid7")
    assert fake.calls == [("GET", f"{BASE}/videos", {"part": "snippet,contentDetails", "id": "vid7"})]


def test_get_track_of_an_unknown_video_is_none(api):
    api([[]])

    assert _provider().get_track("nope", SESSION) is None


# ---------------------------------------------------------------------------
# Taking a like off
# ---------------------------------------------------------------------------


def test_unlike_rates_the_video_none(api):
    fake = api()

    _provider().unlike_saved_track("vid1", SESSION)

    assert fake.calls == [("POST", f"{BASE}/videos/rate", {"id": "vid1", "rating": "none"})]


def test_unlike_without_the_scope_asks_to_reconnect(api):
    fake = api()
    fake.answers.append(fake.response(403, _error("insufficientPermissions"), "POST", f"{BASE}/videos/rate"))

    with pytest.raises(PermissionError, match="reconnect YouTube"):
        _provider().unlike_saved_track("vid1", SESSION)


def test_unlike_scope_error_is_read_from_the_error_details_too(api):
    fake = api()
    body = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Request had insufficient scopes.",
                      "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}
    fake.answers.append(fake.response(403, body, "POST", f"{BASE}/videos/rate"))

    with pytest.raises(PermissionError):
        _provider().unlike_saved_track("vid1", SESSION)


@pytest.mark.parametrize("status, body", [
    (500, _error("backendError", 500)),
    (404, _error("videoNotFound", 404)),
    (403, _error("forbidden")),  # a 403 that is not about the scope is not a reconnect
    (400, None),
])
def test_unlike_other_failures_are_the_http_error(api, status, body):
    fake = api()
    fake.answers.append(fake.response(status, body, "POST", f"{BASE}/videos/rate"))

    with pytest.raises(httpx.HTTPStatusError) as raised:
        _provider().unlike_saved_track("vid1", SESSION)

    assert raised.value.response.status_code == status


def test_unlike_with_an_exhausted_quota_is_rate_limited(api):
    fake = api()
    fake.answers.append(fake.response(403, _error("quotaExceeded"), "POST", f"{BASE}/videos/rate"))

    with pytest.raises(ProviderRateLimited):
        _provider().unlike_saved_track("vid1", SESSION)


# ---------------------------------------------------------------------------
# Failures of the import
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status, reason", [
    (403, "quotaExceeded"), (403, "dailyLimitExceeded"), (403, "rateLimitExceeded"),
    (403, "userRateLimitExceeded"), (429, "rateLimitExceeded"),
])
def test_an_exhausted_quota_is_rate_limited_not_an_empty_library(api, status, reason):
    fake = api()
    fake.answers.append(fake.response(status, _error(reason, status), "GET", f"{BASE}/videos"))

    with pytest.raises(ProviderRateLimited) as raised:
        fetch_all_saved_tracks(_provider(), SESSION)

    # What the runner tells the host: a limit of the service for now, never a refused credential or an empty library.
    assert classify_exception(raised.value).kind == "rate_limited"


def test_a_quota_running_out_mid_import_fails_it_instead_of_returning_the_first_pages(api):
    fake = api(_likes(120))
    fake.override = lambda url, params: (
        fake.response(403, _error("quotaExceeded"), "GET", url) if params.get("pageToken") == "p1" else None)

    with pytest.raises(ProviderRateLimited):
        fetch_all_saved_tracks(_provider(), SESSION)

    assert len(fake.calls) == 2


@pytest.mark.parametrize("status, body", [(401, _error("authError", 401)), (403, _error("insufficientPermissions"))])
def test_a_refused_connection_asks_to_reconnect(api, status, body):
    fake = api()
    fake.answers.append(fake.response(status, body, "GET", f"{BASE}/videos"))

    with pytest.raises(PermissionError, match="reconnect YouTube"):
        fetch_all_saved_tracks(_provider(), SESSION)


def test_a_server_error_is_the_http_error(api):
    fake = api()
    fake.answers.append(fake.response(503, _error("backendError", 503), "GET", f"{BASE}/videos"))

    with pytest.raises(httpx.HTTPStatusError) as failed:
        fetch_all_saved_tracks(_provider(), SESSION)
    assert (classify_exception(failed.value).kind, type(failed.value).__name__) == ("upstream_error", "HTTPStatusError")


def test_test_connection_costs_one_unit_and_reports_a_failure_as_text(api):
    fake = api([[]])

    assert _provider().test_connection(SESSION) is None
    assert fake.calls[0][2] == {"part": "id", "myRating": "like", "maxResults": 1}

    fake.answers.append(fake.response(403, _error("quotaExceeded"), "GET", f"{BASE}/videos"))
    assert "quota" in _provider().test_connection(SESSION)


# ---------------------------------------------------------------------------
# No catalog: nothing but likes ever reaches YouTube
# ---------------------------------------------------------------------------


def _forbid_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"unexpected HTTP request: {args} {kwargs}")

    monkeypatch.setattr(youtube_module.httpx, "get", refuse)
    monkeypatch.setattr(youtube_module.httpx, "post", refuse)


def test_every_catalog_method_answers_empty_without_a_request(monkeypatch):
    _forbid_network(monkeypatch)
    provider = _provider()
    track = make_track("Song", "Artist")

    assert provider.search_tracks("q", SESSION) == []
    assert provider.search_albums("q", SESSION) == []
    assert provider.search_artists("q", SESSION) == []
    assert provider.search_playlists("q", SESSION) == []
    assert provider.get_album("a", SESSION) is None
    assert provider.get_artist("a", SESSION) is None
    assert provider.get_playlist("p", SESSION) is None
    assert provider.get_artist_albums("a", SESSION) == []
    assert provider.get_artist_top_tracks("a", SESSION) == []
    assert provider.find_by_isrc("ISRC", SESSION) is None
    assert provider.find_related_tracks(track, SESSION) == []
    assert provider.find_similar_tracks(track, SESSION) == []
    assert provider.get_preview_url("v", SESSION) is None
    assert provider.get_new_releases(SESSION) == []
    assert provider.get_trending_tracks(SESSION) == []
    assert provider.get_trending_albums(SESSION) == []
    assert provider.get_trending_artists(SESSION) == []
    assert provider.get_saved_albums(SESSION) == []
    assert provider.get_followed_artists(SESSION) == []
    assert provider.get_saved_playlists(SESSION) == []
    assert provider.get_cover_url("c", SESSION) is None
    assert provider.streams_full_tracks is False


def test_a_search_through_the_runner_never_calls_youtube(monkeypatch):
    _forbid_network(monkeypatch)

    for method in ("search_tracks", "search_albums", "search_artists", "search_playlists"):
        assert via_runner(YouTubeMetadataProvider, method, "anything", SESSION, provider_name="youtube") == []


def test_it_does_not_offer_the_sign_in_through_the_provider():
    """From the main repository's ``test_provider_login_providers.py``: a provider that does not declare the sign-in
    keeps the contract's defaults."""
    provider = _provider()

    assert provider.supports_login() is False
    assert provider.identify_account(SESSION) is None
