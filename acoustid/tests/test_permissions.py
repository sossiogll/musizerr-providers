"""The permissions AcoustID declares: what the image says, and that they cover everything the code contacts."""
from __future__ import annotations

import re
from pathlib import Path

import acoustid
import pytest
import requests

import musizerr_provider_acoustid as module
from musizerr_provider_acoustid import AcoustIDProvider
from musizerr_sdk.auth import AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.labels import LABEL_LABEL, declared_from_labels, labels_for
from musizerr_sdk.manifest import build
from musizerr_sdk.permissions import Permissions, canonical, from_json, to_json, validate

from permissions_support import allowed, attempt, destination

PROVIDER_DIR = Path(__file__).resolve().parent.parent
PERMISSIONS = AcoustIDProvider.permissions
SESSION = AuthSession.anonymous()


@pytest.fixture
def provider() -> AcoustIDProvider:
    return AcoustIDProvider(StaticProviderContext(settings={"api_key": "synthetic-key"}, provider_name="acoustid"))


def test_the_permissions_are_exactly_the_acoustid_web_service_and_nothing_else():
    assert PERMISSIONS == Permissions(egress=("api.acoustid.org:443",))
    assert validate(PERMISSIONS) is PERMISSIONS and canonical(PERMISSIONS) == PERMISSIONS
    assert not PERMISSIONS.egress_from_settings and not PERMISSIONS.filesystem_from_settings and not PERMISSIONS.tcp_from_settings


def test_the_permissions_survive_the_label_round_trip(provider):
    labels = labels_for(build(provider))
    declared = declared_from_labels(labels)

    assert declared.permissions == PERMISSIONS == from_json(to_json(declared.permissions))
    assert (declared.name, declared.roles, declared.provider_api_version) == ("acoustid", ("identification",), PROVIDER_API_VERSION)
    assert labels[LABEL_LABEL] == provider.label == "AcoustID"


def test_the_dockerfile_agrees_with_the_provider():
    dockerfile = (PROVIDER_DIR / "Dockerfile").read_text()

    assert re.search(r'org\.musizerr\.provider\.label="AcoustID"', dockerfile)
    assert "musizerr-provider-base:5" in dockerfile
    assert "musizerr_provider_acoustid:AcoustIDProvider" in dockerfile
    assert "org.opencontainers.image.description=" in dockerfile and "org.opencontainers.image.url=" in dockerfile


def test_the_address_the_library_talks_to_is_a_declared_destination():
    """pyacoustid's own default is plain HTTP, and the API key travels in the request: the provider sets HTTPS on the
    library when it is imported. The address the library then contacts is the one the permissions declare, and never
    plain HTTP."""
    assert module._API_BASE_URL == "https://api.acoustid.org/v2/"
    assert acoustid.API_BASE_URL == "https://api.acoustid.org/v2/"
    assert allowed(PERMISSIONS, acoustid.API_BASE_URL)
    assert destination(acoustid.API_BASE_URL) == ("api.acoustid.org", 443)
    assert not allowed(PERMISSIONS, "http://api.acoustid.org/v2/")


def test_no_plain_http_address_is_written_in_the_code():
    assert "http://" not in Path(module.__file__).read_text()


def test_every_request_the_provider_makes_is_a_declared_destination(provider, monkeypatch, tmp_path):
    urls: list[str] = []

    class _Reply:
        def json(self):
            return {"status": "ok", "results": []}

    def post(session, url, data=None, headers=None, timeout=None):
        urls.append(url)
        return _Reply()

    monkeypatch.setattr(requests.Session, "post", post)
    monkeypatch.setattr(acoustid, "fingerprint_file", lambda path, **kw: (10.0, b"AQAAfake"))

    from musizerr_sdk.interfaces import WorkspaceFile

    attempt(
        lambda: provider.test_connection(SESSION),
        lambda: provider.identify(WorkspaceFile(workspace=str(tmp_path), name="a.mp3")),
    )

    assert len(urls) == 2
    for url in urls:
        assert allowed(PERMISSIONS, url), f"{url} is not covered by the permissions"
    assert {destination(url) for url in urls} == {("api.acoustid.org", 443)}
    assert all(url.startswith("https://") for url in urls)


def test_a_destination_that_is_not_declared_is_not_allowed():
    for url in ("http://api.acoustid.org/v2/lookup", "https://acoustid.org/", "https://musicbrainz.org/", "https://api.acoustid.org:8443/"):
        assert not allowed(PERMISSIONS, url)


def test_the_provider_reads_no_environment():
    source = Path(module.__file__).read_text()

    assert "os.environ" not in source and "getenv" not in source
