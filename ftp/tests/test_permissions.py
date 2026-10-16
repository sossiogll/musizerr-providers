"""The permissions the FTP storage declares: what the image says, and that they cover everything the code contacts."""
from __future__ import annotations

import ftplib
import re
from pathlib import Path

import pytest

import musizerr_provider_ftp as module
from musizerr_provider_ftp import FTPStorageProvider
from musizerr_sdk import net
from musizerr_sdk.interfaces import PROVIDER_API_VERSION
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.labels import LABEL_LABEL, declared_from_labels, labels_for
from musizerr_sdk.manifest import build
from musizerr_sdk.permissions import Permissions, TcpAccess, canonical, from_json, tcp_hosts, to_json, validate

from permissions_support import allowed

PROVIDER_DIR = Path(__file__).resolve().parent.parent
PERMISSIONS = FTPStorageProvider.permissions


@pytest.fixture
def provider() -> FTPStorageProvider:
    return FTPStorageProvider(StaticProviderContext(settings={"host": "ftp.lan", "remote_base_path": "/music"}, provider_name="ftp"))


def test_the_permissions_are_raw_tcp_to_the_host_the_user_sets_on_any_port_and_nothing_else():
    assert PERMISSIONS == Permissions(tcp_from_settings=(TcpAccess("host", "any"),))
    assert validate(PERMISSIONS) is PERMISSIONS and canonical(PERMISSIONS) == PERMISSIONS
    assert not PERMISSIONS.egress and not PERMISSIONS.egress_from_settings and not PERMISSIONS.filesystem_from_settings


def test_the_setting_the_permissions_name_is_a_required_setting_of_the_provider(provider):
    schema = {setting.key: setting for setting in provider.config_schema()}

    assert "host" in schema and schema["host"].required is True


def test_the_permissions_survive_the_label_round_trip(provider):
    labels = labels_for(build(provider))
    declared = declared_from_labels(labels)

    assert declared.permissions == PERMISSIONS == from_json(to_json(declared.permissions))
    assert (declared.name, declared.roles, declared.provider_api_version) == ("ftp", ("storage",), PROVIDER_API_VERSION)
    assert labels[LABEL_LABEL] == provider.label == "FTP Storage"


def test_the_dockerfile_agrees_with_the_provider():
    dockerfile = (PROVIDER_DIR / "Dockerfile").read_text()

    assert re.search(r'org\.musizerr\.provider\.label="FTP Storage"', dockerfile)
    assert "musizerr-provider-base:5" in dockerfile
    assert "musizerr_provider_ftp:FTPStorageProvider" in dockerfile
    assert "org.opencontainers.image.description=" in dockerfile and "org.opencontainers.image.url=" in dockerfile


def test_the_host_the_setting_holds_is_the_one_that_is_allowed_on_every_port(provider):
    settings = {"host": "ftp.lan", "port": "2121"}

    assert tcp_hosts(PERMISSIONS, settings) == {"host": ("ftp.lan",)}
    for target in ("ftp.lan:21", "ftp.lan:2121", "ftp.lan:41234", "ftp.lan:65535"):
        assert allowed(PERMISSIONS, target, settings)
    assert not allowed(PERMISSIONS, "other.lan:21", settings)      # another host
    assert not allowed(PERMISSIONS, "ftp.lan:21", {})              # nothing configured: nothing granted
    assert tcp_hosts(PERMISSIONS, {"host": "ftp://files.example.org:2121"}) == {"host": ("files.example.org",)}


def test_the_sockets_of_ftplib_go_through_the_proxy_helper():
    """Without this the library would open its own sockets and a sandbox, which has no network, would refuse them."""
    assert isinstance(ftplib.socket, net._SocketModule)
    assert ftplib.socket.create_connection is net.create_connection
    assert ftplib.socket.AF_INET is not None  # everything else is forwarded to the real socket module


def test_passive_mode_is_the_only_one_used():
    source = Path(module.__file__).read_text()

    assert "set_pasv(False)" not in source and "makeport" not in source


def test_the_provider_reads_no_environment():
    source = Path(module.__file__).read_text()

    assert "os.environ" not in source and "getenv" not in source
