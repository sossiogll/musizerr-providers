"""
MusicBrainz as a package, with the HTTP answers simulated.

Ported from the main repository (where the provider lived in the application):

- ``test_related_foreign_ids.py``: ``find_related_tracks`` asks for the variants of a track of ANOTHER provider by
  an id it can use, never by the foreign ``provider_id`` (MusicBrainz was asked ``/recording/4045817471``, another catalog's
  id, and answered 400 on every track page);
- ``test_artist_albums_paging.py`` (its MusicBrainz case): ``get_artist_albums`` honours a large ``limit`` by paging.

The tests under "Added here" cover what the main repository only exercised through its host.
"""
from __future__ import annotations

import httpx
import pytest

import musizerr_provider_musicbrainz as musicbrainz
from helpers import Http, make_track, via_runner
from musizerr_provider_musicbrainz import MusicBrainzMetadataProvider
from musizerr_sdk.auth import AuthFlow, AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.identifiers import ISRC, MBID, PROVIDER_ID, Identifier

SESSION = AuthSession(flow=AuthFlow.NONE)
AUTH = AuthSession.anonymous()
RECORDING = "b1a9c0e9-d987-4042-ae91-78d6a3267d69"
RELEASE = "0f7a9e0e-2a3c-4b37-9bb4-0d7a0e1a2c11"

_RECORDING_BODY = {
    "id": RECORDING, "title": "Song", "length": 200000,
    "artist-credit": [{"name": "Artist", "artist": {"id": "a1", "name": "Artist"}}],
    "isrcs": ["USUM72601821"],
    "releases": [{"id": RELEASE, "title": "Album", "date": "2020"},
                 {"id": "other-release", "title": "Best Of", "date": "2022"}],
}


# ---------------------------------------------------------------------------
# test_related_foreign_ids.py
# ---------------------------------------------------------------------------


@pytest.fixture
def mb(monkeypatch):
    http = Http({f"/recording/{RECORDING}": _RECORDING_BODY})
    monkeypatch.setattr(musicbrainz.httpx, "get", http)
    return MusicBrainzMetadataProvider(StaticProviderContext()), http


def test_a_foreign_track_without_an_id_it_can_use_is_not_looked_up(mb):
    provider, http = mb
    track = make_track("Song", "Artist", provider="other", provider_id="4045817471")

    assert provider.find_related_tracks(track, SESSION) == []
    assert http.calls == []


def test_a_foreign_track_is_looked_up_by_its_recording_mbid(mb):
    provider, http = mb
    track = make_track("Song", "Artist", provider="other", provider_id="4045817471")
    track.add_id(Identifier(type=MBID, value=RECORDING))

    related = provider.find_related_tracks(track, SESSION)

    assert [url for url, _ in http.calls] == [f"{musicbrainz._BASE}/recording/{RECORDING}"]
    assert {t.album for t in related} == {"Album", "Best Of"}


def test_a_foreign_track_carrying_the_providers_own_id_uses_it(mb):
    provider, http = mb
    track = make_track("Song", "Artist", provider="other", provider_id="4045817471")
    track.add_id(Identifier(type=PROVIDER_ID, value=f"{RECORDING}::{RELEASE}", provider=provider.name))

    related = provider.find_related_tracks(track, SESSION)

    assert http.calls and http.calls[0][0].endswith(f"/recording/{RECORDING}")
    # The track itself (same recording on the same release) is not a variant.
    assert [t.album for t in related] == ["Best Of"]


def test_its_own_track_still_uses_its_provider_id(mb):
    provider, http = mb
    track = make_track("Song", "Artist", provider=provider.name, provider_id=f"{RECORDING}::{RELEASE}")

    related = provider.find_related_tracks(track, SESSION)

    assert [t.album for t in related] == ["Best Of"]


# ---------------------------------------------------------------------------
# test_artist_albums_paging.py (MusicBrainz)
# ---------------------------------------------------------------------------


class Pages:
    """Replaces ``httpx.get``: answers every call with the slice of ``total`` items the request asks for (by
    ``offset``/``limit``), through ``make``, and records the params."""

    def __init__(self, monkeypatch, total: int, make):
        self.total = total
        self.make = make
        self.calls: list[dict] = []
        monkeypatch.setattr(httpx, "get", self._get)

    def _get(self, url, **kwargs):
        params = kwargs.get("params") or {}
        self.calls.append(params)
        start = params.get("index", params.get("offset", 0))
        end = min(start + params["limit"], self.total)
        return httpx.Response(200, json=self.make(start, end, self.total), request=httpx.Request("GET", url))


def test_musicbrainz_pages_through_a_long_discography(monkeypatch):
    def page(start, end, total):
        groups = [{"id": f"rg{i}", "title": f"Record {i}", "primary-type": "Album"} for i in range(start, end)]
        return {"release-groups": groups, "release-group-count": total}

    http = Pages(monkeypatch, 230, page)

    albums = MusicBrainzMetadataProvider(StaticProviderContext()).get_artist_albums("mb", AUTH, limit=500)

    assert len(albums) == 230
    assert [(c["offset"], c["limit"]) for c in http.calls] == [(0, 100), (100, 100), (200, 100)]
    # EPs are the artist's releases too.
    assert http.calls[0]["type"] == "album|single|ep"


# ---------------------------------------------------------------------------
# Added here: the mapping and the requests
# ---------------------------------------------------------------------------


@pytest.fixture
def provider():
    return MusicBrainzMetadataProvider(StaticProviderContext(provider_name="musicbrainz"))


def test_a_recording_on_two_releases_is_two_tracks_with_a_compound_id(provider, monkeypatch):
    http = Http({"/recording": {"recordings": [_RECORDING_BODY]}})
    monkeypatch.setattr(musicbrainz.httpx, "get", http)

    tracks = provider.search_tracks("song artist", SESSION, limit=7)

    assert [t.provider_id for t in tracks] == [f"{RECORDING}::{RELEASE}", f"{RECORDING}::other-release"]
    assert [t.album for t in tracks] == ["Album", "Best Of"] and [t.year for t in tracks] == [2020, 2022]
    first = tracks[0]
    assert (first.title, first.artist, first.duration_ms, first.album_provider_id) == ("Song", "Artist", 200000, RELEASE)
    assert {(i.type, i.value) for i in first.external_ids} == {(MBID, RECORDING), (ISRC, "USUM72601821")}
    assert http.calls == [(f"{musicbrainz._BASE}/recording", {"query": "song artist", "limit": 7, "fmt": "json"})]


def test_every_request_identifies_the_application_with_a_user_agent(provider, monkeypatch):
    http = Http({"/artist": {"artists": [{"id": "a1", "name": "Artist"}]}})
    monkeypatch.setattr(musicbrainz.httpx, "get", http)

    artists = provider.search_artists("artist", SESSION)

    assert [(a.name, a.provider_id) for a in artists] == [("Artist", "a1")]
    assert http.headers[0]["User-Agent"].startswith("Musizerr/")


def test_a_track_is_found_by_its_recording_and_release(provider, monkeypatch):
    monkeypatch.setattr(musicbrainz.httpx, "get", Http({f"/recording/{RECORDING}": _RECORDING_BODY}))

    track = provider.get_track(f"{RECORDING}::other-release", SESSION)

    assert track.album == "Best Of"
    assert provider.get_track(f"{RECORDING}::nowhere", SESSION) is None
    assert provider.get_track(RECORDING, SESSION) is None  # two releases: no way to choose one


def test_an_unknown_isrc_is_none_not_an_error(provider, monkeypatch):
    monkeypatch.setattr(musicbrainz.httpx, "get", Http({}))  # every path: 404

    assert provider.find_by_isrc("USUM72601821", SESSION) is None


def test_an_album_asked_by_release_group_id_falls_back_to_its_first_release(provider, monkeypatch):
    release = {
        "id": RELEASE, "title": "Album", "date": "2020-05-01",
        "artist-credit": [{"name": "Artist", "artist": {"id": "a1", "name": "Artist"}}],
        "media": [{"position": 1, "tracks": [{"position": 3, "title": "Song", "length": 1000,
                                              "recording": {"id": RECORDING, "title": "Song", "length": 1000}}]}],
    }
    # /release/rg1 is a 404 (the id is a release group's): the fake answers a 404 for what it does not know.
    http = Http({"/release-group/rg1": {"releases": [{"id": RELEASE}]}, f"/release/{RELEASE}": release})
    monkeypatch.setattr(musicbrainz.httpx, "get", http)

    album = provider.get_album("rg1", SESSION)

    assert [url.rsplit("/", 2)[-2:] for url, _ in http.calls] == [["release", "rg1"], ["release-group", "rg1"], ["release", RELEASE]]
    assert (album.title, album.year, album.track_count) == ("Album", 2020, 1)
    assert [(t.title, t.track_number, t.disc_number) for t in album.tracks] == [("Song", 3, 1)]


def test_the_declarations_the_host_reads(provider):
    assert provider.supports_isrc_lookup() and provider.supports_discography()
    assert provider.auth.flow == AuthFlow.NONE
    assert [setting.key for setting in provider.config_schema()] == ["search_limit"]


def test_a_search_travels_through_the_runner_as_it_does_in_the_sandbox(monkeypatch):
    monkeypatch.setattr(musicbrainz.httpx, "get", Http({"/recording": {"recordings": [_RECORDING_BODY]}}))

    tracks = via_runner(MusicBrainzMetadataProvider, "search_tracks", "song", SESSION, provider_name="musicbrainz")

    assert [t.album for t in tracks] == ["Album", "Best Of"]


def test_a_refusal_of_the_service_reaches_the_host_as_a_classified_failure(monkeypatch):
    monkeypatch.setattr(musicbrainz.httpx, "get", Http({"/recording": {}}, status=503))

    with pytest.raises(Exception) as raised:
        via_runner(MusicBrainzMetadataProvider, "search_tracks", "song", SESSION, provider_name="musicbrainz")

    assert raised.value.kind == "upstream_error"
