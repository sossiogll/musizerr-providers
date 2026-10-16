"""The permissions Navidrome declares: what the image says, and that they cover everything the code contacts."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import musizerr_provider_navidrome as module
from helpers import make_track
from musizerr_provider_navidrome import NavidromeProvider
from musizerr_sdk.auth import AuthFlow, AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.labels import LABEL_LABEL, declared_from_labels, labels_for
from musizerr_sdk.manifest import build
from musizerr_sdk.permissions import Permissions, canonical, from_json, settings_targets, to_json, validate

from permissions_support import allowed, attempt, destination, record_http

PROVIDER_DIR = Path(__file__).resolve().parent.parent
PERMISSIONS = NavidromeProvider.permissions
# Written in the module but contacted by nobody: the example of an address in the description of the `url` setting.
NOT_CONTACTED = {"http://navidrome:4533"}
SERVER = "http://music.lan:4533"
SETTINGS = {"url": SERVER, "mode": "full"}
SESSION = AuthSession(flow=AuthFlow.CREDENTIALS, extra={"username": "alice", "password": "pw"})


@pytest.fixture
def provider() -> NavidromeProvider:
    return NavidromeProvider(StaticProviderContext(settings=SETTINGS, provider_name="navidrome"))


def test_the_permissions_are_the_url_the_user_sets_and_nothing_else():
    assert PERMISSIONS == Permissions(egress_from_settings=("url",))
    assert validate(PERMISSIONS) is PERMISSIONS and canonical(PERMISSIONS) == PERMISSIONS
    assert not PERMISSIONS.egress and not PERMISSIONS.filesystem_from_settings and not PERMISSIONS.tcp_from_settings


def test_the_setting_the_permissions_name_is_a_required_setting_of_the_provider(provider):
    schema = {setting.key: setting for setting in provider.config_schema()}

    assert set(PERMISSIONS.egress_from_settings) <= set(schema) and schema["url"].required is True


def test_the_permissions_survive_the_label_round_trip(provider):
    labels = labels_for(build(provider))
    declared = declared_from_labels(labels)

    assert declared.permissions == PERMISSIONS == from_json(to_json(declared.permissions))
    assert (declared.name, declared.roles, declared.provider_api_version) == ("navidrome", ("metadata", "sync"), PROVIDER_API_VERSION)
    assert labels[LABEL_LABEL] == provider.label == "Navidrome"


def test_the_dockerfile_agrees_with_the_provider():
    dockerfile = (PROVIDER_DIR / "Dockerfile").read_text()

    assert re.search(r'org\.musizerr\.provider\.label="Navidrome"', dockerfile)
    assert "musizerr-provider-base:5" in dockerfile
    assert "musizerr_provider_navidrome:NavidromeProvider" in dockerfile
    assert "org.opencontainers.image.description=" in dockerfile and "org.opencontainers.image.url=" in dockerfile


def test_no_address_is_written_in_the_code_but_an_example():
    urls = set(re.findall(r"https?://[^\s\"'`)>\]]+", Path(module.__file__).read_text()))

    assert urls <= NOT_CONTACTED


def test_every_request_the_provider_makes_goes_to_the_server_the_user_set(provider, monkeypatch, tmp_path):
    recorder = record_http(monkeypatch)

    attempt(
        lambda: provider.test_connection(SESSION),
        lambda: provider.identify_account(SESSION),
        lambda: provider.search_tracks("q", SESSION),
        lambda: provider.search_albums("q", SESSION),
        lambda: provider.search_artists("q", SESSION),
        lambda: provider.get_track("s1", SESSION),
        lambda: provider.get_album("a1", SESSION),
        lambda: provider.get_artist("r1", SESSION),
        lambda: provider.get_artist_albums("r1", SESSION),
        lambda: provider.get_saved_tracks(SESSION),
        lambda: provider.trigger_scan(SESSION),
        lambda: provider.find_candidates(make_track(), SESSION),
        lambda: provider.like_track("s1", SESSION),
        lambda: provider.unlike_track("s1", SESSION),
        lambda: provider.create_playlist("n", ["s1"], SESSION),
        lambda: provider.update_playlist("p1", "n", ["s1"], SESSION),
        lambda: provider.delete_playlist("p1", SESSION),
        lambda: provider.list_library(SESSION),
        lambda: provider.fetch_partial("s1", str(tmp_path), SESSION, max_bytes=10),
    )

    assert len(recorder.urls) >= 19
    assert {destination(url) for url in recorder.urls} == {("music.lan", 4533)}
    for url in recorder.urls:
        assert allowed(PERMISSIONS, url, SETTINGS), f"{url} is not covered by the permissions"


def test_the_url_setting_grants_whatever_the_user_types_and_nothing_before():
    assert settings_targets(PERMISSIONS, {"url": "http://192.168.1.20:4533/"}) == ("192.168.1.20:4533",)
    assert settings_targets(PERMISSIONS, {"url": "https://navidrome.example.org"}) == ("navidrome.example.org:443",)
    assert settings_targets(PERMISSIONS, {"url": "music.lan"}) == ("music.lan:80", "music.lan:443")
    assert settings_targets(PERMISSIONS, {}) == ()
    assert not allowed(PERMISSIONS, f"{SERVER}/rest/ping")                    # no URL configured: no grant
    assert allowed(PERMISSIONS, f"{SERVER}/rest/ping", SETTINGS)
    assert not allowed(PERMISSIONS, "http://other.lan:4533/rest/ping", SETTINGS)
    assert not allowed(PERMISSIONS, "https://example.com/", SETTINGS)


def test_the_provider_reads_no_environment():
    source = Path(module.__file__).read_text()

    assert "os.environ" not in source and "getenv" not in source
