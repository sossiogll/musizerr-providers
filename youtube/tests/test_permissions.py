"""The permissions YouTube declares: what the image says, and that they cover everything the code contacts."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import musizerr_provider_youtube as module
from musizerr_provider_youtube import YouTubeMetadataProvider
from musizerr_sdk.auth import AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.labels import LABEL_LABEL, declared_from_labels, labels_for
from musizerr_sdk.manifest import build
from musizerr_sdk.permissions import Permissions, canonical, from_json, to_json, validate

from permissions_support import allowed, attempt, destination, record_http

PROVIDER_DIR = Path(__file__).resolve().parent.parent
SESSION = AuthSession.oauth2(headers={"Authorization": "Bearer token"})
OAUTH2 = YouTubeMetadataProvider.auth.oauth2
# The sign-in pages Google serves. The HOST sends the user there and exchanges the code for a token
# (``AuthManager``); the provider is never the one that contacts them, so they are not in its egress.
NOT_CONTACTED = {OAUTH2.authorize_url, OAUTH2.token_url}


@pytest.fixture
def provider() -> YouTubeMetadataProvider:
    return YouTubeMetadataProvider(StaticProviderContext(provider_name="youtube"))


def test_the_permissions_are_exactly_the_data_api_and_nothing_else():
    permissions = YouTubeMetadataProvider.permissions

    assert permissions == Permissions(egress=("www.googleapis.com:443",))
    assert validate(permissions) is permissions and canonical(permissions) == permissions


def test_the_permissions_survive_the_label_round_trip(provider):
    labels = labels_for(build(provider))
    declared = declared_from_labels(labels)

    assert declared.permissions == YouTubeMetadataProvider.permissions == from_json(to_json(declared.permissions))
    assert (declared.name, declared.roles, declared.provider_api_version) == ("youtube", ("metadata",), PROVIDER_API_VERSION)
    assert labels[LABEL_LABEL] == provider.label == "YouTube"


def test_the_dockerfile_agrees_with_the_provider():
    dockerfile = (PROVIDER_DIR / "Dockerfile").read_text()

    assert re.search(r'org\.musizerr\.provider\.label="YouTube"', dockerfile)
    assert "musizerr-provider-base:5" in dockerfile
    assert "musizerr_provider_youtube:YouTubeMetadataProvider" in dockerfile
    assert "org.opencontainers.image.description=" in dockerfile and "org.opencontainers.image.url=" in dockerfile


def test_every_address_written_in_the_code_is_a_declared_destination_or_one_the_host_visits():
    urls = set(re.findall(r"https?://[^\s\"'`)>\]]+", Path(module.__file__).read_text()))

    assert module._BASE in urls and NOT_CONTACTED <= urls
    for url in urls - NOT_CONTACTED:
        assert allowed(YouTubeMetadataProvider.permissions, url), f"{url} is written in the code but not declared"
    for url in NOT_CONTACTED:
        assert not allowed(YouTubeMetadataProvider.permissions, url), f"{url} is visited by the host, not by the provider"


def test_every_request_the_provider_makes_is_a_declared_destination(provider, monkeypatch):
    recorder = record_http(monkeypatch)

    attempt(
        lambda: provider.test_connection(SESSION),
        lambda: provider.get_track("vid1", SESSION),
        lambda: list(provider.iter_saved_tracks(SESSION)),
        lambda: provider.get_saved_tracks(SESSION),
        lambda: provider.unlike_saved_track("vid1", SESSION),
    )

    assert len(recorder.urls) >= 5
    for url in recorder.urls:
        assert allowed(YouTubeMetadataProvider.permissions, url), f"{url} is not covered by the permissions"
    assert {destination(url) for url in recorder.urls} == {("www.googleapis.com", 443)}


def test_a_destination_that_is_not_declared_is_not_allowed():
    for url in ("https://example.com/", "http://www.googleapis.com/youtube/v3", "https://youtube.googleapis.com/", "https://i.ytimg.com/vi/x.jpg"):
        assert not allowed(YouTubeMetadataProvider.permissions, url)


def test_the_provider_reads_no_environment():
    source = Path(module.__file__).read_text()

    assert "os.environ" not in source and "getenv" not in source
