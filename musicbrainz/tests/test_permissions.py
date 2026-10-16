"""The permissions MusicBrainz declares: what the image says, and that they cover everything the code contacts."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import musizerr_provider_musicbrainz as module
from musizerr_provider_musicbrainz import MusicBrainzMetadataProvider
from musizerr_sdk.auth import AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.labels import LABEL_LABEL, declared_from_labels, labels_for
from musizerr_sdk.manifest import build
from musizerr_sdk.permissions import Permissions, canonical, from_json, to_json, validate

from helpers import make_track
from permissions_support import allowed, attempt, destination, record_http

PROVIDER_DIR = Path(__file__).resolve().parent.parent
SESSION = AuthSession.anonymous()
# Written in the module but contacted by nobody: the address in the User-Agent that names the application.
NOT_CONTACTED = {"https://github.com/musizerr/musizerr"}


@pytest.fixture
def provider() -> MusicBrainzMetadataProvider:
    return MusicBrainzMetadataProvider(StaticProviderContext(provider_name="musicbrainz"))


def test_the_permissions_are_exactly_one_fixed_host_and_nothing_else():
    permissions = MusicBrainzMetadataProvider.permissions

    assert permissions == Permissions(egress=("musicbrainz.org:443",))
    assert validate(permissions) is permissions and canonical(permissions) == permissions


def test_the_permissions_survive_the_label_round_trip(provider):
    labels = labels_for(build(provider))
    declared = declared_from_labels(labels)

    assert declared.permissions == MusicBrainzMetadataProvider.permissions == from_json(to_json(declared.permissions))
    assert (declared.name, declared.roles, declared.provider_api_version) == ("musicbrainz", ("metadata",), PROVIDER_API_VERSION)
    assert labels[LABEL_LABEL] == provider.label == "MusicBrainz"


def test_the_dockerfile_agrees_with_the_provider():
    dockerfile = (PROVIDER_DIR / "Dockerfile").read_text()

    assert re.search(r'org\.musizerr\.provider\.label="MusicBrainz"', dockerfile)
    assert "musizerr-provider-base:5" in dockerfile
    assert "musizerr_provider_musicbrainz:MusicBrainzMetadataProvider" in dockerfile
    assert "org.opencontainers.image.description=" in dockerfile and "org.opencontainers.image.url=" in dockerfile


def test_every_address_written_in_the_code_is_a_declared_destination_or_not_contacted():
    urls = set(re.findall(r"https?://[^\s\"'`)>\]]+", Path(module.__file__).read_text()))

    assert module._BASE in urls
    for url in urls - NOT_CONTACTED:
        assert allowed(MusicBrainzMetadataProvider.permissions, url), f"{url} is written in the code but not declared"


def test_every_request_the_provider_makes_is_a_declared_destination(provider, monkeypatch):
    recorder = record_http(monkeypatch)
    track = make_track(provider="musicbrainz", provider_id="rec::rel", mbid="rec")

    attempt(
        lambda: provider.test_connection(SESSION),
        lambda: provider.search_tracks("q", SESSION),
        lambda: provider.search_albums("q", SESSION),
        lambda: provider.search_artists("q", SESSION),
        lambda: provider.find_by_isrc("USUM72601821", SESSION),
        lambda: provider.find_related_tracks(track, SESSION),
        lambda: provider.get_track("rec::rel", SESSION),
        lambda: provider.get_artist("a1", SESSION),
        lambda: provider.get_artist_albums("a1", SESSION),
        lambda: provider.get_album("rel", SESSION),
    )

    assert len(recorder.urls) >= 10
    for url in recorder.urls:
        assert allowed(MusicBrainzMetadataProvider.permissions, url), f"{url} is not covered by the permissions"
    assert {destination(url) for url in recorder.urls} == {("musicbrainz.org", 443)}


def test_a_destination_that_is_not_declared_is_not_allowed():
    for url in ("https://example.com/", "http://musicbrainz.org/ws/2", "https://coverartarchive.org/", "https://musicbrainz.org:8443/"):
        assert not allowed(MusicBrainzMetadataProvider.permissions, url)


def test_the_provider_reads_no_environment():
    source = Path(module.__file__).read_text()

    assert "os.environ" not in source and "getenv" not in source
