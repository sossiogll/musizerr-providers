"""
The Navidrome provider (metadata and sync, on the Subsonic API), with a fake Subsonic server.

Ported from the main repository, where these facts were asserted through the host and with fixtures recorded from a
real server. The fixtures here are synthetic (``subsonic.py``), the assertions are the provider's:

- ``test_playlist_providers.py`` (its Subsonic half) -> "Playlists";
- ``test_provider_login_providers.py`` (its Navidrome half) -> "Sign-in";
- ``test_track_numbers.py`` (``test_navidrome_reads_track_and_disc_number``) and
  ``test_public_urls.py`` (the cover and stream URLs carry the prefix) -> "Mapping";
- ``test_duplicates_identity.py`` (the parts about ``list_library``: the bit rate in kbps, two files with one virtual
  path, remixes of one song are separate items) and ``test_track_identity_golden.py`` (the ISRC of a Subsonic song)
  -> "Library listing" and "Mapping". What the host makes of the listing (duplicate groups, identity) stays there.

"Added here" has what the main repository did not test directly.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

import httpx
import pytest

import musizerr_provider_navidrome as module
from helpers import RemoteFailure, make_track, via_runner
from musizerr_provider_navidrome import NavidromeProvider
from musizerr_sdk.auth import AccountIdentity, AuthFlow, AuthSession
from musizerr_sdk.capabilities import CAPABILITY_USER_LIBRARY
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.errors import RemotePlaylistGone
from musizerr_sdk.identifiers import ISRC, PROVIDER_ID
from musizerr_sdk.manifest import build

from subsonic import Subsonic, song, without_auth

SETTINGS = {"url": "http://navi.test:4533/", "mode": "full"}
ACCOUNT = AuthSession(flow=AuthFlow.CREDENTIALS, extra={"username": "alice", "password": "pw"})


def _navidrome(monkeypatch, answers=None, settings=None, **context) -> tuple[NavidromeProvider, Subsonic]:
    server = Subsonic(answers)
    monkeypatch.setattr(module.httpx, "get", server)
    provider = NavidromeProvider(StaticProviderContext(settings=settings or SETTINGS, provider_name="navidrome", **context))
    return provider, server


# ======================================================================
# Playlists (test_playlist_providers.py)
# ======================================================================


def test_a_playlist_is_created_with_its_tracks_in_order(monkeypatch):
    provider, server = _navidrome(monkeypatch, {"createPlaylist": {"status": "ok", "playlist": {"id": "pl-7"}}})

    remote_id = provider.create_playlist("Drive", ["s1", "s2"], ACCOUNT)

    assert remote_id == "pl-7" and provider.supports_playlists()
    assert [(name, without_auth(params)) for name, params in server.calls] == [
        ("createPlaylist", {"name": "Drive", "songId": ["s1", "s2"]})]
    assert server.calls[0][1]["u"] == "alice"


def test_an_update_replaces_the_content_and_renames(monkeypatch):
    provider, server = _navidrome(monkeypatch)

    provider.update_playlist("pl-7", "Night", ["s2", "s1"], ACCOUNT)

    assert [(name, without_auth(params)) for name, params in server.calls] == [
        ("createPlaylist", {"playlistId": "pl-7", "songId": ["s2", "s1"]}),
        ("updatePlaylist", {"playlistId": "pl-7", "name": "Night"}),
    ]


def test_a_playlist_is_deleted_by_its_id(monkeypatch):
    provider, server = _navidrome(monkeypatch)

    provider.delete_playlist("pl-7", ACCOUNT)

    assert [(name, without_auth(params)) for name, params in server.calls] == [("deletePlaylist", {"id": "pl-7"})]


def test_a_playlist_the_server_does_not_have_is_gone(monkeypatch):
    gone = {"status": "failed", "error": {"code": 70, "message": "not found"}}
    provider, _ = _navidrome(monkeypatch, {"createPlaylist": gone, "deletePlaylist": gone})

    with pytest.raises(RemotePlaylistGone):
        provider.update_playlist("pl-7", "Night", [], ACCOUNT)
    with pytest.raises(RemotePlaylistGone):
        provider.delete_playlist("pl-7", ACCOUNT)


def test_any_other_failure_of_the_server_is_an_error_not_an_empty_answer(monkeypatch):
    provider, _ = _navidrome(monkeypatch, {"createPlaylist": {"status": "failed", "error": {"code": 40, "message": "wrong credentials"}}})
    with pytest.raises(RuntimeError, match="wrong credentials"):
        provider.create_playlist("Drive", [], ACCOUNT)

    provider, _ = _navidrome(monkeypatch, {"createPlaylist": {"status": "ok"}})
    with pytest.raises(RuntimeError, match="without the playlist"):
        provider.create_playlist("Drive", [], ACCOUNT)


def test_the_server_declares_the_playlists_and_the_likes_it_can_keep(monkeypatch):
    provider, _ = _navidrome(monkeypatch)
    manifest = build(provider)

    assert manifest.declarations["supports_playlists"] is True and manifest.declarations["supports_likes"] is True
    assert manifest.declarations["supports_user_library"] is True
    assert set(manifest.roles) == {"metadata", "sync"}


# ======================================================================
# Sign-in (test_provider_login_providers.py)
# ======================================================================

FORM = AuthSession(flow=AuthFlow.CREDENTIALS, extra={"username": "alice", "password": "pw"})


def test_it_identifies_the_user_the_server_says_the_credentials_belong_to(monkeypatch):
    provider, server = _navidrome(monkeypatch, {"getUser": {"status": "ok", "user": {"username": "Alice"}}},
                                  settings={"url": "http://navi.test", "mode": "meta"})

    assert provider.supports_login() is True
    assert provider.identify_account(FORM) == AccountIdentity("Alice", None)
    endpoint, params = server.calls[0]
    assert server.urls[0] == "http://navi.test/rest/getUser" and params["u"] == "alice" and params["username"] == "alice"


def test_it_does_not_recognise_credentials_the_server_refuses(monkeypatch):
    provider, _ = _navidrome(monkeypatch, {"getUser": {"status": "failed", "error": {"code": 40, "message": "Wrong username or password"}}})

    assert provider.identify_account(FORM) is None


# ======================================================================
# Mapping (test_track_numbers.py, test_public_urls.py, test_track_identity_golden.py)
# ======================================================================


def test_navidrome_reads_track_and_disc_number():
    provider = NavidromeProvider(StaticProviderContext())

    track = provider._to_track({"id": "s", "title": "T", "artist": "A", "track": 9, "discNumber": 1})

    assert (track.track_number, track.disc_number) == (9, 1)


@pytest.mark.parametrize("prefix", ["/api", "/music/api"])
def test_the_cover_and_stream_urls_carry_the_public_prefix(prefix):
    provider = NavidromeProvider(StaticProviderContext(provider_name="navidrome", api_prefix=prefix))

    track = provider._to_track({"id": "song-1", "coverArt": "al-9", "title": "T", "artist": "A"})

    assert track.cover_url == f"{prefix}/cover/provider/{provider.name}/al-9"
    assert track.stream_url == f"{prefix}/stream/provider/{provider.name}/song-1"


def test_a_subsonic_song_carries_its_isrc_and_its_own_id(monkeypatch):
    provider, server = _navidrome(monkeypatch, {"search3": {"status": "ok", "searchResult3": {"song": [
        song(1, title="Example Song", isrc=["XX0000000001"]),
    ]}}})

    [found] = provider.search_tracks("Example Song", ACCOUNT, limit=7)

    assert found.get_id(ISRC).value == "XX0000000001"
    assert found.get_id(PROVIDER_ID, "navidrome").value == "song-1"
    assert (found.artist, found.album, found.duration_ms, found.year) == ("Example Artist", "Example Album", 201_000, 2020)
    assert (found.artist_provider_id, found.album_provider_id) == ("artist-1", "album-1")
    assert [(a.name, a.provider_id) for a in found.artists] == [("Example Artist", "artist-1")]
    assert without_auth(server.calls[0][1]) == {"query": "Example Song", "songCount": 7, "albumCount": 0, "artistCount": 0}


def test_several_isrcs_in_one_entry_are_split():
    provider = NavidromeProvider(StaticProviderContext())

    track = provider._to_track(song(1, isrc=["XX0000000001; XX0000000002", " XX0000000003 "]))

    assert [i.value for i in track.external_ids if i.type == ISRC] == ["XX0000000001", "XX0000000002", "XX0000000003"]


def test_an_album_comes_with_its_tracks(monkeypatch):
    provider, _ = _navidrome(monkeypatch, {"getAlbum": {"status": "ok", "album": {
        "id": "album-1", "name": "Example Album", "artist": "Example Artist", "artistId": "artist-1", "year": 2020,
        "songCount": 2, "coverArt": "mf-album-1", "song": [song(1), song(2)],
    }}})

    album = provider.get_album("album-1", ACCOUNT)

    assert (album.title, album.artist, album.year, album.track_count) == ("Example Album", "Example Artist", 2020, 2)
    assert [t.title for t in album.tracks] == ["Track 1", "Track 2"]


def test_an_unknown_id_is_none(monkeypatch):
    provider, _ = _navidrome(monkeypatch, {"getSong": {"status": "failed", "error": {"code": 70, "message": "not found"}}})

    assert provider.get_track("nope", ACCOUNT) is None
    assert provider.get_artist("nope", ACCOUNT) is None


def test_the_albums_of_an_artist_and_the_artists_found(monkeypatch):
    provider, _ = _navidrome(monkeypatch, {
        "getArtist": {"status": "ok", "artist": {"id": "artist-1", "name": "Example Artist", "album": [
            {"id": "album-1", "name": "Example Album", "artist": "Example Artist", "songCount": 9, "year": 2020},
        ]}},
        "search3": {"status": "ok", "searchResult3": {"artist": [{"id": "artist-1", "name": "Example Artist"}],
                                                      "album": [{"id": "album-1", "name": "Example Album"}]}},
    })

    assert [a.title for a in provider.get_artist_albums("artist-1", ACCOUNT)] == ["Example Album"]
    assert [a.name for a in provider.search_artists("exa", ACCOUNT)] == ["Example Artist"]
    assert [a.title for a in provider.search_albums("exa", ACCOUNT)] == ["Example Album"]


# ======================================================================
# Library listing (test_duplicates_identity.py)
# ======================================================================


def test_the_subsonic_bitrate_is_read_in_kbps_and_given_in_bit_per_second(monkeypatch):
    provider, _ = _navidrome(monkeypatch, {"search3": {"status": "ok", "searchResult3": {"song": [
        song(1, bitRate=320), song(2, bitRate=0), song(3, bitRate=None),
    ]}}})

    items = provider.list_library(ACCOUNT)

    assert [item.bitrate for item in items] == [320_000, 0, 0]


def test_two_files_with_one_virtual_path_are_two_items_told_apart_by_their_sync_id(monkeypatch):
    twin = "Example Artist/Example Album/04 - Twin.mp3"
    provider, _ = _navidrome(monkeypatch, {"search3": {"status": "ok", "searchResult3": {"song": [
        song(1, id="twin-a", title="Twin", path=twin), song(2, id="twin-b", title="Twin", path=twin),
    ]}}})

    items = provider.list_library(ACCOUNT)

    assert len(items) == 2 and len({item.path for item in items}) == 1
    assert [item.sync_id for item in items] == ["twin-a", "twin-b"]
    assert {item.bitrate for item in items} == {320_000}


def test_the_versions_of_one_song_are_separate_items_with_their_own_titles(monkeypatch):
    titles = ["Anthem", "Anthem (Remastered 2021)", "Anthem (Club Remix)", "Anthem (Acoustic)"]
    provider, _ = _navidrome(monkeypatch, {"search3": {"status": "ok", "searchResult3": {"song": [
        song(n, title=title) for n, title in enumerate(titles, 1)
    ]}}})

    assert [item.title for item in provider.list_library(ACCOUNT)] == titles


def test_a_library_listing_carries_what_the_server_indexed(monkeypatch):
    provider, _ = _navidrome(monkeypatch, {"search3": {"status": "ok", "searchResult3": {"song": [
        song(1, suffix="FLAC", samplingRate=96000, bitRate=1000, size=31_000_000, year=None, album=None),
    ]}}})

    (item,) = provider.list_library(ACCOUNT)

    assert (item.sync_id, item.suffix, item.sample_rate, item.bitrate, item.size, item.album) == (
        "song-1", "flac", 96000, 1_000_000, 31_000_000, "")
    assert item.path.endswith("01 - Track 1.mp3") and item.duration_ms == 201_000


def test_the_library_is_read_page_by_page_until_a_short_page(monkeypatch):
    songs = [song(n) for n in range(1, 1201)]

    def page(params):
        offset = int(params["songOffset"])
        return {"status": "ok", "searchResult3": {"song": songs[offset:offset + params["songCount"]]}}

    provider, server = _navidrome(monkeypatch, {"search3": page})

    items = provider.list_library(ACCOUNT)

    assert len(items) == 1200
    assert [(p["songOffset"], p["songCount"], p["query"]) for _, p in server.calls] == [(0, 500, ""), (500, 500, ""), (1000, 500, "")]


# ======================================================================
# Added here: likes, scan, candidates, URLs, the partial download
# ======================================================================


def test_the_starred_songs_are_the_saved_tracks_with_their_time_in_naive_utc(monkeypatch):
    provider, server = _navidrome(monkeypatch, {"getStarred2": {"status": "ok", "starred2": {"song": [
        song(1, starred="2024-03-05T10:20:30.123Z"), song(2, starred="not a date"), song(3),
    ]}}})

    saved = provider.get_saved_tracks(ACCOUNT, limit=2, offset=0)

    assert [t.provider_id for t in saved] == ["song-1", "song-2"]
    assert saved[0].saved_at == datetime(2024, 3, 5, 10, 20, 30, 123000) and saved[1].saved_at is None
    assert [t.provider_id for t in provider.get_saved_tracks(ACCOUNT, limit=5, offset=2)] == ["song-3"]


def test_liking_scanning_and_testing_the_connection_are_one_call_each(monkeypatch):
    provider, server = _navidrome(monkeypatch)

    provider.like_track("s1", ACCOUNT)
    provider.unlike_track("s1", ACCOUNT)
    provider.trigger_scan(ACCOUNT)
    assert provider.test_connection(ACCOUNT) is None

    assert [(name, without_auth(params)) for name, params in server.calls] == [
        ("star", {"id": "s1"}), ("unstar", {"id": "s1"}), ("startScan", {}), ("ping", {}),
    ]


def test_the_connection_test_says_why_it_failed(monkeypatch):
    def refuse(url, params=None, timeout=None, **kwargs):
        raise httpx.ConnectError("no route to host", request=httpx.Request("GET", url))

    monkeypatch.setattr(module.httpx, "get", refuse)
    provider = NavidromeProvider(StaticProviderContext(settings=SETTINGS))

    assert "no route to host" in provider.test_connection(ACCOUNT)


def test_a_subsonic_error_status_is_an_empty_answer_for_the_browse_calls(monkeypatch):
    provider, _ = _navidrome(monkeypatch, {"search3": {"status": "failed", "error": {"code": 40, "message": "wrong credentials"}}})

    assert provider.search_tracks("x", ACCOUNT) == []
    assert provider.list_library(ACCOUNT) == []


def test_the_candidates_are_asked_with_the_title_without_its_version_tail(monkeypatch):
    provider, server = _navidrome(monkeypatch, {"search3": {"status": "ok", "searchResult3": {"song": [song(1), song(2)]}}})

    candidates = provider.find_candidates(make_track(title="Track 1 (Remastered 2011)"), ACCOUNT)

    assert [(c.sync_id, c.track.title) for c in candidates] == [("song-1", "Track 1"), ("song-2", "Track 2")]
    assert server.calls[0][1]["query"] == "Track 1"


def test_the_subsonic_credentials_are_a_salted_token_and_never_the_password(monkeypatch):
    provider, server = _navidrome(monkeypatch)

    provider.like_track("s1", ACCOUNT)

    params = server.calls[0][1]
    assert params["u"] == "alice" and "p" not in params and "pw" not in params.values()
    assert params["t"] == hashlib.md5(("pw" + params["s"]).encode()).hexdigest()
    assert (params["c"], params["f"]) == ("musizerr", "json")


def test_the_stream_and_cover_urls_point_at_the_server_the_user_set():
    provider = NavidromeProvider(StaticProviderContext(settings=SETTINGS))

    stream = provider.get_stream_url("song-1", ACCOUNT)
    cover = provider.get_cover_url("mf-album-1", ACCOUNT)

    assert stream.startswith("http://navi.test:4533/rest/stream?") and "id=song-1" in stream and "u=alice" in stream
    assert cover.startswith("http://navi.test:4533/rest/getCoverArt?") and "id=mf-album-1" in cover and "size=300" in cover


class _Download:
    """A stand-in for ``httpx.stream``: the response of a file download, in blocks."""

    def __init__(self, blocks: list[bytes], content_type: str = "audio/mpeg", status: int = 200):
        self.blocks, self.content_type, self.status = blocks, content_type, status
        self.urls: list[str] = []

    def __call__(self, method, url, **kwargs):
        self.urls.append(url)
        outer = self

        class _Context:
            def __enter__(self):
                return httpx.Response(outer.status, headers={"content-type": outer.content_type}, content=b"".join(outer.blocks),
                                      request=httpx.Request(method, url))

            def __exit__(self, *exc):
                return False

        return _Context()


def test_the_original_file_is_downloaded_not_streamed_and_cut_at_the_limit(monkeypatch, tmp_path):
    download = _Download([bytes(300_000)])
    monkeypatch.setattr(module.httpx, "stream", download)
    provider = NavidromeProvider(StaticProviderContext(settings=SETTINGS))

    name = provider.fetch_partial("song-1", str(tmp_path), ACCOUNT, max_bytes=70_000)

    assert name == "partial.mp3"
    # Read in blocks of 64 KiB: it stops at the first block that reaches the limit.
    assert (tmp_path / name).stat().st_size == 2 * 65_536
    assert "/rest/download?" in download.urls[0] and "id=song-1" in download.urls[0]


@pytest.mark.parametrize("content_type, status", [("application/json", 200), ("text/xml", 200), ("audio/mpeg", 404)])
def test_a_structured_error_instead_of_audio_is_no_file(monkeypatch, tmp_path, content_type, status):
    monkeypatch.setattr(module.httpx, "stream", _Download([b"{}"], content_type, status))
    provider = NavidromeProvider(StaticProviderContext(settings=SETTINGS))

    assert provider.fetch_partial("song-1", str(tmp_path), ACCOUNT) is None
    assert list(tmp_path.iterdir()) == []


def test_a_listing_travels_through_the_runner_as_it_does_in_the_sandbox(monkeypatch):
    monkeypatch.setattr(module.httpx, "get", Subsonic({"search3": {"status": "ok", "searchResult3": {"song": [song(1)]}}}))

    items = via_runner(NavidromeProvider, "list_library", ACCOUNT, settings=SETTINGS, provider_name="navidrome")

    assert [(item.sync_id, item.bitrate) for item in items] == [("song-1", 320_000)]


def test_an_unreachable_server_reaches_the_host_as_a_classified_failure(monkeypatch):
    def refuse(url, params=None, timeout=None, **kwargs):
        raise httpx.ConnectError("no route to host", request=httpx.Request("GET", url))

    monkeypatch.setattr(module.httpx, "get", refuse)

    with pytest.raises(RemoteFailure) as failed:
        via_runner(NavidromeProvider, "search_tracks", "x", ACCOUNT, settings=SETTINGS, provider_name="navidrome")

    assert failed.value.kind == "unreachable"


def test_the_settings_and_their_validation():
    provider = NavidromeProvider(StaticProviderContext())

    assert {s.key for s in provider.config_schema()} == {"url", "mode"}
    assert provider.validate_config({"url": "http://x", "mode": "full"}) and provider.validate_config({"url": "http://x", "mode": "meta"})
    assert not provider.validate_config({"url": "http://x", "mode": "other"}) and not provider.validate_config({"mode": "full"})
