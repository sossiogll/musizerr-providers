"""The permissions Lidarr declares: what the image says, and that they cover everything the code contacts."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import musizerr_provider_lidarr as module
from helpers import make_track
from musizerr_provider_lidarr import LidarrDownloadProvider
from musizerr_sdk.auth import AuthFlow, AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.delegation import FileRef
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.labels import LABEL_LABEL, declared_from_labels, labels_for
from musizerr_sdk.manifest import build
from musizerr_sdk.models import Album
from musizerr_sdk.permissions import Permissions, canonical, from_json, settings_targets, to_json, validate

from permissions_support import allowed, attempt, destination, record_http

PROVIDER_DIR = Path(__file__).resolve().parent.parent
PERMISSIONS = LidarrDownloadProvider.permissions
# Written in the module but contacted by nobody: the address in MusicBrainz' User-Agent that names the
# application, and the example of a Lidarr address in the description of the `url` setting.
NOT_CONTACTED = {"https://github.com/sossiogll/musizerr", "http://localhost:8686"}
LIDARR_URL = "http://lidarr.lan:8686"
SESSION = AuthSession(flow=AuthFlow.API_KEY, params={"apikey": "k"}, extra={"url": LIDARR_URL})


@pytest.fixture
def provider() -> LidarrDownloadProvider:
    return LidarrDownloadProvider(StaticProviderContext(settings={"url": LIDARR_URL}, provider_name="lidarr"))


def test_the_permissions_are_musicbrainz_and_the_url_the_user_sets():
    assert PERMISSIONS == Permissions(egress=("musicbrainz.org:443",), egress_from_settings=("url",))
    assert validate(PERMISSIONS) is PERMISSIONS and canonical(PERMISSIONS) == PERMISSIONS
    assert not PERMISSIONS.filesystem_from_settings and not PERMISSIONS.tcp_from_settings


def test_the_setting_the_permissions_name_is_a_setting_of_the_provider(provider):
    keys = {setting.key for setting in provider.config_schema()}

    assert set(PERMISSIONS.egress_from_settings) <= keys


def test_the_permissions_survive_the_label_round_trip(provider):
    labels = labels_for(build(provider))
    declared = declared_from_labels(labels)

    assert declared.permissions == PERMISSIONS == from_json(to_json(declared.permissions))
    assert (declared.name, declared.roles, declared.provider_api_version) == ("lidarr", ("download",), PROVIDER_API_VERSION)
    assert labels[LABEL_LABEL] == provider.label == "Lidarr"


def test_the_dockerfile_agrees_with_the_provider():
    dockerfile = (PROVIDER_DIR / "Dockerfile").read_text()

    assert re.search(r'org\.musizerr\.provider\.label="Lidarr"', dockerfile)
    assert "musizerr-provider-base:5" in dockerfile
    assert "musizerr_provider_lidarr:LidarrDownloadProvider" in dockerfile
    assert "org.opencontainers.image.description=" in dockerfile and "org.opencontainers.image.url=" in dockerfile


def test_every_address_written_in_the_code_is_a_declared_destination_or_only_an_example():
    urls = set(re.findall(r"https?://[^\s\"'`)>\]]+", Path(module.__file__).read_text()))

    assert module._MB_BASE in urls
    for url in urls - NOT_CONTACTED:
        assert allowed(PERMISSIONS, url), f"{url} is written in the code but not declared"


def test_every_request_the_provider_makes_is_a_declared_destination(provider, monkeypatch):
    recorder = record_http(monkeypatch)
    track = make_track(title="Song", artist="Artist", album="Album")

    attempt(
        lambda: provider.test_connection(SESSION),
        lambda: provider.download_fuzzy(track, "/unused", SESSION),
        lambda: provider.dispatch_album(Album(title="Album", artist="Artist"), SESSION),
        lambda: provider.poll_delegation("5", SESSION),
        lambda: provider.list_delegated_tracks("5", SESSION),
        lambda: provider.list_delegated_files("5", SESSION),
        lambda: provider.delete_file(FileRef(path="a", external_id="3"), SESSION),
        lambda: provider.request_rescan(SESSION),
        lambda: provider.register_push_endpoint("http://musizerr/hook", SESSION),
    )

    hosts = {destination(url) for url in recorder.urls}
    assert ("lidarr.lan", 8686) in hosts and ("musicbrainz.org", 443) in hosts
    assert hosts == {("lidarr.lan", 8686), ("musicbrainz.org", 443)}
    for url in recorder.urls:
        assert allowed(PERMISSIONS, url, {"url": LIDARR_URL}), f"{url} is not covered by the permissions"


def test_the_url_setting_grants_whatever_the_user_types_and_nothing_before():
    assert settings_targets(PERMISSIONS, {"url": "http://192.168.1.20:8686/"}) == ("192.168.1.20:8686",)
    assert settings_targets(PERMISSIONS, {"url": "https://lidarr.example.org"}) == ("lidarr.example.org:443",)
    assert settings_targets(PERMISSIONS, {}) == ()
    assert not allowed(PERMISSIONS, "http://lidarr.lan:8686/api/v1/system/status")  # no URL configured: no grant
    assert allowed(PERMISSIONS, "http://lidarr.lan:8686/api/v1/system/status", {"url": LIDARR_URL})
    assert not allowed(PERMISSIONS, "http://other.lan:8686/api/v1/system/status", {"url": LIDARR_URL})


def test_a_destination_that_is_not_declared_is_not_allowed():
    for url in ("https://example.com/", "http://musicbrainz.org/ws/2", "https://api.musicbrainz.org/", "https://musicbrainz.org:8443/"):
        assert not allowed(PERMISSIONS, url, {"url": LIDARR_URL})


def test_the_provider_reads_no_environment():
    source = Path(module.__file__).read_text()

    assert "os.environ" not in source and "getenv" not in source
