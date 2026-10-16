"""
``StorageProvider.list_files`` of the FTP storage: the relative path of every file saved, walking the server with
MLSD or, where the server does not know it, with NLST.

Ported from the main repository's ``test_storage_listing.py`` (its FTP half; the local storage's half is in the
local provider's tests).
"""
from __future__ import annotations

import ftplib
import os
from pathlib import PurePosixPath

import pytest

from musizerr_provider_ftp import FTPStorageProvider
from musizerr_sdk.context import StaticProviderContext




class _FakeFTP:
    """Server FTP in memoria: ``files`` sono i path assoluti dei file, le
    cartelle sono i loro antenati."""

    def __init__(self, files: list[str], *, mlsd: bool = True) -> None:
        self.files = {PurePosixPath(f) for f in files}
        self.dirs = {p for f in self.files for p in f.parents}
        self.supports_mlsd = mlsd
        self.cwd_path = PurePosixPath("/")
        self.closed = False
        self.denied: set[PurePosixPath] = set()

    def _abs(self, path: str) -> PurePosixPath:
        return PurePosixPath(os.path.normpath(str(self.cwd_path / path)))

    def _children(self, directory: PurePosixPath) -> list[PurePosixPath]:
        return sorted(p for p in self.files | self.dirs if p.parent == directory and p != directory)

    def cwd(self, path: str) -> None:
        target = self._abs(path)
        if target not in self.dirs:
            raise ftplib.error_perm("550 not a directory")
        self.cwd_path = target

    def pwd(self) -> str:
        return str(self.cwd_path)

    def mlsd(self, path: str, facts=None):
        if not self.supports_mlsd:
            raise ftplib.error_perm("500 MLSD not understood")
        directory = self._abs(path)
        if directory in self.denied:
            raise ftplib.error_perm("550 Permission denied")
        yield ".", {"type": "cdir"}
        for child in self._children(directory):
            yield child.name, {"type": "dir" if child in self.dirs else "file"}

    def nlst(self, path: str) -> list[str]:
        children = self._children(self._abs(path))
        if not children:
            raise ftplib.error_perm("550 No files found")
        return [str(PurePosixPath(path) / c.name) for c in children]

    def quit(self) -> None:
        self.closed = True


class _FTPWithServer(FTPStorageProvider):
    def __init__(self, server: _FakeFTP) -> None:
        super().__init__(StaticProviderContext(settings={"host": "ftp.test", "remote_base_path": "/music"}))
        self.server = server

    def _connect(self, session, config=None):
        return self.server


_REMOTE = ["/music/A/One/01 song.flac", "/music/A/One/cover.jpg", "/music/B/song.mp3", "/other/x.mp3"]


@pytest.mark.parametrize("mlsd", [True, False], ids=["mlsd", "nlst"])
def test_ftp_storage_lists_every_file_under_its_base_path(mlsd):
    server = _FakeFTP(_REMOTE + ["/music/Empty/.keep"], mlsd=mlsd)
    server.files.discard(PurePosixPath("/music/Empty/.keep"))  # cartella vuota

    files = _FTPWithServer(server).list_files("", session=None)

    assert sorted(files) == ["A/One/01 song.flac", "A/One/cover.jpg", "B/song.mp3"]
    assert server.closed


@pytest.mark.parametrize("mlsd", [True, False], ids=["mlsd", "nlst"])
def test_ftp_storage_lists_only_under_a_prefix(mlsd):
    files = _FTPWithServer(_FakeFTP(_REMOTE, mlsd=mlsd)).list_files("A", session=None)

    assert sorted(files) == ["A/One/01 song.flac", "A/One/cover.jpg"]


def test_an_unreadable_ftp_folder_is_skipped_and_the_walk_goes_on():
    server = _FakeFTP(_REMOTE)
    server.denied.add(PurePosixPath("/music/A"))

    assert list(_FTPWithServer(server).list_files("", session=None)) == ["B/song.mp3"]


def test_an_unreachable_ftp_base_path_is_an_error_and_closes_the_connection():
    server = _FakeFTP(["/elsewhere/x.mp3"])

    with pytest.raises(ftplib.error_perm):
        _FTPWithServer(server).list_files("", session=None)
    assert server.closed
