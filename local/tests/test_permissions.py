"""The permissions the local storage declares: what the image says, and that they cover everything the code touches."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import musizerr_provider_local as module
from musizerr_provider_local import LocalStorageProvider
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.labels import LABEL_LABEL, declared_from_labels, labels_for
from musizerr_sdk.manifest import build
from musizerr_sdk.permissions import FilesystemAccess, Permissions, canonical, from_json, path_values, to_json, validate

PROVIDER_DIR = Path(__file__).resolve().parent.parent
PERMISSIONS = LocalStorageProvider.permissions


@pytest.fixture
def provider() -> LocalStorageProvider:
    return LocalStorageProvider(StaticProviderContext(settings={"root_dir": "/downloads"}, provider_name="local"))


def test_the_permissions_are_the_library_folder_read_and_write_and_nothing_else():
    assert PERMISSIONS == Permissions(filesystem_from_settings=(FilesystemAccess("root_dir", "rw"),))
    assert validate(PERMISSIONS) is PERMISSIONS and canonical(PERMISSIONS) == PERMISSIONS
    assert not PERMISSIONS.egress and not PERMISSIONS.egress_from_settings and not PERMISSIONS.tcp_from_settings


def test_the_setting_the_permissions_name_is_the_one_the_code_reads(provider):
    assert {setting.key for setting in provider.config_schema()} == {"root_dir"}
    assert 'setting("root_dir")' in Path(module.__file__).read_text()
    assert path_values(PERMISSIONS, {"root_dir": "/downloads"}) == {"root_dir": "/downloads"}
    assert path_values(PERMISSIONS, {}) == {}


def test_the_permissions_survive_the_label_round_trip(provider):
    labels = labels_for(build(provider))
    declared = declared_from_labels(labels)

    assert declared.permissions == PERMISSIONS == from_json(to_json(declared.permissions))
    assert (declared.name, declared.roles, declared.provider_api_version) == ("local", ("storage",), PROVIDER_API_VERSION)
    assert labels[LABEL_LABEL] == provider.label == "Local Storage"


def test_the_dockerfile_agrees_with_the_provider():
    dockerfile = (PROVIDER_DIR / "Dockerfile").read_text()

    assert re.search(r'org\.musizerr\.provider\.label="Local Storage"', dockerfile)
    assert "musizerr-provider-base:5" in dockerfile
    assert "musizerr_provider_local:LocalStorageProvider" in dockerfile
    assert "org.opencontainers.image.description=" in dockerfile and "org.opencontainers.image.url=" in dockerfile


def test_the_code_touches_no_network_and_reads_no_environment():
    source = Path(module.__file__).read_text()

    for name in ("socket", "httpx", "urllib", "requests", "ftplib", "http.client"):
        assert not re.search(rf"^\s*(import|from)\s+{re.escape(name)}\b", source, re.M), name
    assert "os.environ" not in source and "getenv" not in source


def test_it_reaches_only_paths_under_the_folder(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    local = LocalStorageProvider(StaticProviderContext(settings={"root_dir": str(root)}))

    for relative in ("../x", "/etc/passwd", "a/../../x", ".", ""):
        with pytest.raises(ValueError):
            local.exists(relative, None)
