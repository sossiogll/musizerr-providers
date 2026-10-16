"""The permissions LRCLIB declares: what the image says, and that they cover everything the code contacts."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import musizerr_provider_lrclib as module
from musizerr_provider_lrclib import LrclibLyricsProvider
from musizerr_sdk.auth import AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.labels import LABEL_LABEL, declared_from_labels, labels_for
from musizerr_sdk.manifest import build
from musizerr_sdk.models import Track
from musizerr_sdk.permissions import Permissions, canonical, from_json, to_json, validate

from permissions_support import allowed, destination, record_http

PROVIDER_DIR = Path(__file__).resolve().parent.parent
SESSION = AuthSession.anonymous()


@pytest.fixture
def provider() -> LrclibLyricsProvider:
    return LrclibLyricsProvider(StaticProviderContext(provider_name="lrclib"))


def test_the_permissions_are_exactly_one_fixed_host_and_nothing_else():
    permissions = LrclibLyricsProvider.permissions

    assert permissions == Permissions(egress=("lrclib.net:443",))
    assert validate(permissions) is permissions
    assert canonical(permissions) == permissions
    assert not permissions.egress_from_settings and not permissions.filesystem_from_settings
    assert not permissions.tcp_from_settings and permissions.limits == Permissions().limits


def test_the_permissions_survive_the_label_round_trip(provider):
    manifest = build(provider)
    labels = labels_for(manifest)
    declared = declared_from_labels(labels)

    assert declared.permissions == LrclibLyricsProvider.permissions == from_json(to_json(LrclibLyricsProvider.permissions))
    assert (declared.name, declared.roles, declared.provider_api_version) == ("lrclib", ("lyrics",), PROVIDER_API_VERSION)
    assert labels[LABEL_LABEL] == provider.label == "LRCLIB"


def test_the_dockerfile_agrees_with_the_provider():
    dockerfile = (PROVIDER_DIR / "Dockerfile").read_text()

    assert re.search(r'org\.musizerr\.provider\.label="LRCLIB"', dockerfile)
    assert "musizerr-provider-base:5" in dockerfile
    assert "musizerr_provider_lrclib:LrclibLyricsProvider" in dockerfile
    assert "org.opencontainers.image.description=" in dockerfile and "org.opencontainers.image.url=" in dockerfile


def test_every_address_written_in_the_code_is_a_declared_destination():
    source = (Path(module.__file__)).read_text()
    urls = set(re.findall(r"https?://[^\s\"'`)>\]]+", source))

    assert module._BASE in urls
    for url in urls:
        assert allowed(LrclibLyricsProvider.permissions, url), f"{url} is written in the code but not declared"


def test_every_request_the_provider_makes_is_a_declared_destination(provider, monkeypatch):
    recorder = record_http(monkeypatch)

    provider.test_connection(SESSION)
    provider.get_lyrics(Track(title="Song", artist="Artist", album="Album", duration_ms=1000), SESSION)

    assert len(recorder.urls) >= 3  # the connection test, the exact match and the search
    for url in recorder.urls:
        assert allowed(LrclibLyricsProvider.permissions, url), f"{url} is not covered by the permissions"
    assert {destination(url) for url in recorder.urls} == {("lrclib.net", 443)}


def test_a_destination_that_is_not_declared_is_not_allowed():
    permissions = LrclibLyricsProvider.permissions

    for url in ("https://example.com/", "http://lrclib.net/api", "https://api.lrclib.net/", "https://lrclib.net:8443/"):
        assert not allowed(permissions, url)


def test_the_provider_uses_no_host_infrastructure_and_no_environment():
    source = Path(module.__file__).read_text()

    assert "os.environ" not in source and "getenv" not in source
