"""
Listening to a track stored over FTP: ``FTPStorageProvider.stream`` used to
download the WHOLE file into the workspace before the host sent the first
byte, so a large FLAC stayed silent for as long as the transfer took. It now
hands the host the bytes as they arrive (``chunks``, with the file size),
and a seek (a Range request) restarts the transfer at that offset (FTP
``REST``): the provider declares ``supports_stream_offset``, the host passes
the offset and answers 206.

Ported from the main repository's ``test_ftp_streaming.py``: the provider's half (what ``stream`` hands over). What the
host makes of it (the 206 and 416 answers, the workspace) stays there.
"""
from __future__ import annotations

import ftplib
from pathlib import PurePosixPath

import pytest
from musizerr_sdk.context import StaticProviderContext
from musizerr_provider_ftp import FTPStorageProvider

AUDIO = bytes(range(256)) * 1024  # 256 KiB
BLOCK = 8192


class _DataConnection:
    """The data socket of one RETR: hands out blocks, counts what was read."""

    def __init__(self, server: "_FakeFTP", data: bytes) -> None:
        self.server = server
        self.data = data
        self.closed = False

    def recv(self, size: int) -> bytes:
        block = self.data[:min(size, BLOCK)]
        self.data = self.data[len(block):]
        self.server.transferred += len(block)
        return block

    def close(self) -> None:
        self.closed = True

    def settimeout(self, seconds) -> None:
        self.timeout = seconds


class _FakeFTP:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = {PurePosixPath(k): v for k, v in files.items()}
        self.transferred = 0
        self.rest: int | None = None
        self.connection: _DataConnection | None = None
        self.quit_called = False
        self.close_called = False
        self.replies_read = 0
        self.sock = None
        self.timeout = None

    def _file(self, remote: str) -> bytes:
        try:
            return self.files[PurePosixPath(remote)]
        except KeyError:
            raise ftplib.error_perm(f"550 {remote}: No such file or directory") from None

    def voidcmd(self, cmd: str) -> str:
        return "200 OK"

    def size(self, remote: str) -> int:
        return len(self._file(remote))

    def transfercmd(self, cmd: str, rest=None) -> _DataConnection:
        data = self._file(cmd.removeprefix("RETR "))
        self.rest = rest
        self.connection = _DataConnection(self, data[int(rest or 0):])
        return self.connection

    def retrbinary(self, cmd: str, callback, blocksize=BLOCK, rest=None) -> str:
        connection = self.transfercmd(cmd, rest)
        while block := connection.recv(blocksize):
            callback(block)
        return "226 Transfer complete"

    def voidresp(self) -> str:
        self.replies_read += 1
        return "226 Transfer complete"

    def quit(self) -> None:
        self.quit_called = True

    def close(self) -> None:
        self.close_called = True


class _FTP(FTPStorageProvider):
    def __init__(self, server: _FakeFTP) -> None:
        super().__init__(StaticProviderContext(settings={"host": "ftp.test", "remote_base_path": "/music"}))
        self.server = server

    def _connect(self, session, config=None, *, listening=False):
        return self.server


@pytest.fixture
def server():
    return _FakeFTP({"/music/A/song.flac": AUDIO})


def test_the_first_bytes_are_handed_over_before_the_whole_file_is_transferred(server, tmp_path):
    result = _FTP(server).stream("A/song.flac", str(tmp_path), session=None)

    assert server.transferred == 0
    first = next(result.chunks)
    assert first == AUDIO[:BLOCK]
    assert server.transferred == BLOCK
    assert result.size == len(AUDIO)
    assert result.media_type == "audio/flac"
    assert first + b"".join(result.chunks) == AUDIO
    # A transfer read to the end: the server's reply is read, the session closed.
    assert server.replies_read == 1 and server.quit_called


def test_a_seek_restarts_the_transfer_at_the_offset(server, tmp_path):
    result = _FTP(server).stream("A/song.flac", str(tmp_path), session=None, offset=100_000)

    assert server.rest == 100_000
    assert result.offset == 100_000
    assert b"".join(result.chunks) == AUDIO[100_000:]


def test_a_listener_who_leaves_early_closes_the_connection_without_waiting(server, tmp_path):
    result = _FTP(server).stream("A/song.flac", str(tmp_path), session=None)
    next(result.chunks)
    result.chunks.close()

    assert server.connection.closed
    assert server.close_called and server.replies_read == 0


def test_a_missing_file_fails_before_any_byte(server, tmp_path):
    with pytest.raises(ftplib.error_perm):
        _FTP(server).stream("A/missing.flac", str(tmp_path), session=None)


