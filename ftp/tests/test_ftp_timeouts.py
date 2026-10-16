"""
Which timeout each step of an FTP session waits.

Ported from the main repository (they tested the provider when it lived in the core):
connecting and logging in are quick steps and wait the short timeout, a transfer is a long
one; a stream waits at most `stream_start_timeout` for everything before its first byte
(connect, login, opening the transfer) and then the download timeout while the data flows,
so a storage that does not answer is known as unreachable in seconds, while a slow
transfer is not cut short.
"""
from __future__ import annotations

from musizerr_sdk.auth import AuthFlow, AuthSession
from musizerr_sdk.context import StaticProviderContext

import musizerr_provider_ftp as ftp_module
from musizerr_provider_ftp import FTPStorageProvider

START_TIMEOUT = 3


def test_the_ftp_storage_connects_with_the_short_timeout_and_transfers_with_the_long_one(monkeypatch):
    seen = {}

    class _FTP:
        sock = None
        timeout = None

        def connect(self, host, port, timeout):
            seen["connect"] = timeout

        def login(self, user, passwd):
            seen["transfer"] = self.timeout

    monkeypatch.setattr(ftp_module.ftplib, "FTP", _FTP)
    provider = FTPStorageProvider(StaticProviderContext(
        settings={"host": "nas", "remote_base_path": "/music"}, connect_timeout=7, download_timeout=60))

    provider._connect(AuthSession(flow=AuthFlow.CREDENTIALS, extra={"username": "u", "password": "p"}))

    assert seen == {"connect": 7, "transfer": 60}


def test_the_ftp_stream_waits_the_start_timeout_until_the_transfer_runs(monkeypatch):
    seen: dict[str, object] = {}

    class _Socket:
        def settimeout(self, seconds):
            seen["control_after_start"] = seconds

    class _Data:
        def settimeout(self, seconds):
            seen["data"] = seconds

        def recv(self, size):
            return b""

        def close(self):
            pass

    class _FTP:
        timeout = None
        sock = None

        def connect(self, host, port, timeout):
            seen["connect"] = timeout
            self.sock = _Socket()

        def login(self, user, passwd):
            seen["login"] = self.timeout

        def voidcmd(self, cmd):
            return "200"

        def size(self, remote):
            return 10

        def transfercmd(self, cmd, rest=None):
            seen["open_transfer"] = self.timeout
            return _Data()

        def voidresp(self):
            return "226"

        def quit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(ftp_module.ftplib, "FTP", _FTP)
    provider = FTPStorageProvider(StaticProviderContext(
        settings={"host": "nas", "remote_base_path": "/music"},
        connect_timeout=10, download_timeout=60, stream_start_timeout=START_TIMEOUT,
    ))

    result = provider.stream("A/song.ogg", "/unused", AuthSession(flow=AuthFlow.CREDENTIALS, extra={}))
    if result.chunks is not None:
        list(result.chunks)

    assert seen == {
        "connect": START_TIMEOUT, "login": START_TIMEOUT, "open_transfer": START_TIMEOUT,
        "control_after_start": 60, "data": 60,
    }
