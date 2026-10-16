"""LRCLIB as a package: against a static context, and behind a runner like in its sandbox."""
from __future__ import annotations

import httpx
import pytest

import musizerr_provider_lrclib as lrclib
from musizerr_provider_lrclib import LrclibLyricsProvider, _parse_lrc, _to_lyrics
from musizerr_sdk import protocol
from musizerr_sdk.auth import AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.labels import declared_from_labels, labels_for
from musizerr_sdk.manifest import build
from musizerr_sdk.models import Lyrics, Track
from musizerr_sdk.runner import ProviderRunner

SESSION = AuthSession.anonymous()
TRACK = Track(title="Song", artist="Artist", album="Album", duration_ms=201_000)
DATA = {"plainLyrics": "la la\nlo lo", "syncedLyrics": "[00:01.50] la la\n[01:02.345] lo lo\nnot a line"}


def response(status: int, body, url: str = "https://lrclib.net/api/get") -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", url))


@pytest.fixture
def provider() -> LrclibLyricsProvider:
    return LrclibLyricsProvider(StaticProviderContext(provider_name="lrclib"))


def serve(monkeypatch, *answers):
    """Replace httpx.get with the given answers (responses or exceptions), in order."""
    calls = []
    queue = list(answers)

    def get(url, **kwargs):
        calls.append((url, kwargs))
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(lrclib.httpx, "get", get)
    return calls


def test_a_synced_lyric_is_parsed_into_timed_lines():
    lines = _parse_lrc(DATA["syncedLyrics"])

    assert [(line.time_ms, line.text) for line in lines] == [(1500, " la la"), (62_345, " lo lo")]


def test_a_response_without_lyrics_is_no_lyrics():
    assert _to_lyrics({"plainLyrics": None, "syncedLyrics": ""}) is None
    lyrics = _to_lyrics(DATA)
    assert lyrics.plain == "la la\nlo lo" and lyrics.source == "lrclib" and len(lyrics.synced) == 2


def test_the_exact_match_is_asked_first_with_the_track_as_parameters(provider, monkeypatch):
    calls = serve(monkeypatch, response(200, DATA))

    lyrics = provider.get_lyrics(TRACK, SESSION)

    assert lyrics.plain == "la la\nlo lo" and len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://lrclib.net/api/get" and kwargs["params"]["duration"] == 201
    assert kwargs["params"]["artist_name"] == "Artist" and kwargs["timeout"] == provider.context.http_timeout


def test_when_there_is_no_exact_match_the_search_is_used(provider, monkeypatch):
    calls = serve(monkeypatch, response(404, {}), response(200, [DATA], "https://lrclib.net/api/search"))

    assert provider.get_lyrics(TRACK, SESSION).plain == "la la\nlo lo"
    assert calls[1][0].endswith("/search") and calls[1][1]["params"] == {"q": "Artist Song"}


def test_a_failure_of_the_service_is_no_lyrics_not_an_error(provider, monkeypatch):
    serve(monkeypatch, httpx.ConnectError("down"), httpx.ConnectError("down"))
    assert provider.get_lyrics(TRACK, SESSION) is None

    serve(monkeypatch, response(500, {}), response(200, []))
    assert provider.get_lyrics(TRACK, SESSION) is None


def test_the_connection_test_says_what_went_wrong(provider, monkeypatch):
    serve(monkeypatch, response(200, []))
    assert provider.test_connection(SESSION) is None

    serve(monkeypatch, httpx.ConnectError("no route"))
    assert "no route" in provider.test_connection(SESSION)


# ------------------------------------------------------------------
# What the host and the gateway read of it
# ------------------------------------------------------------------


def test_the_manifest_is_a_lyrics_provider_that_declares_its_one_destination(provider):
    manifest = build(provider)

    assert (manifest.name, manifest.roles, manifest.provider_api_version) == ("lrclib", ("lyrics",), PROVIDER_API_VERSION)
    assert manifest.permissions.egress == ("lrclib.net:443",)
    assert not manifest.permissions.egress_from_settings and not manifest.permissions.filesystem_from_settings


def test_the_image_labels_say_the_same(provider):
    declared = declared_from_labels(labels_for(build(provider)))

    assert declared.name == "lrclib" and declared.roles == ("lyrics",)
    assert declared.permissions.egress == ("lrclib.net:443",)


def test_lyrics_travel_through_the_runner_as_they_do_in_the_sandbox(monkeypatch):
    serve(monkeypatch, response(200, DATA))
    runner = ProviderRunner(LrclibLyricsProvider)

    request = protocol.call_request("get_lyrics", (TRACK, SESSION), {}, StaticProviderContext(provider_name="lrclib").snapshot())
    reply = protocol.read_reply(runner.handle(request), lambda remote: RuntimeError(remote.message))

    assert isinstance(reply, Lyrics) and reply.plain == "la la\nlo lo" and reply.synced[0].time_ms == 1500
