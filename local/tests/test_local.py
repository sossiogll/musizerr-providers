"""
The local storage, against real folders.

Ported from the main repository:

- ``test_storage_listing.py`` (its local half): ``list_files`` gives the path of every file relative to the library root;
- ``test_storage_streaming.py`` (``test_local_storage_links_the_library_file_into_the_workspace``): ``stream`` puts the
  library file in the call's workspace as a hard link;
- ``test_download_to_library.py`` (``test_local_storage_uses_its_own_root_setting``): ``store``;
- ``test_library_path_safety.py`` (the two ``test_local_storage_*`` tests): a path outside the folder is refused.

"Added here" has what runs differently in a sandbox, where the library folder and the workspace are two different
mounts.
"""
from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest

from musizerr_provider_local import LocalStorageProvider
from musizerr_sdk.auth import AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.interfaces import WorkspaceFile


# ------------------------------------------------------------------
# test_storage_listing.py
# ------------------------------------------------------------------


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    for rel in ("A/One/01 song.flac", "A/One/cover.jpg", "B/song.mp3", ".musizerr-work/x/part.flac"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x")
    return root


def _local(root) -> LocalStorageProvider:
    return LocalStorageProvider(StaticProviderContext(settings={"root_dir": str(root)}, provider_name="local"))


def test_local_storage_lists_every_file_relative_to_the_library_root(library):
    files = _local(library).list_files("", session=None)

    assert sorted(files) == [
        ".musizerr-work/x/part.flac", "A/One/01 song.flac", "A/One/cover.jpg", "B/song.mp3",
    ]


def test_local_storage_lists_only_under_a_prefix(library):
    assert sorted(_local(library).list_files("A", session=None)) == ["A/One/01 song.flac", "A/One/cover.jpg"]


def test_local_storage_does_not_descend_into_a_linked_folder(library):
    os.symlink(library / "A", library / "B" / "loop")

    assert "B/loop/One/01 song.flac" not in list(_local(library).list_files("", session=None))


def test_a_missing_local_library_is_an_error_not_an_empty_library(tmp_path):
    with pytest.raises(FileNotFoundError):
        _local(tmp_path / "missing").list_files("", session=None)



# ------------------------------------------------------------------
# test_storage_streaming.py, test_download_to_library.py, test_library_path_safety.py
# ------------------------------------------------------------------


@pytest.fixture
def stocked(tmp_path):
    root = tmp_path / "library"
    (root / "A").mkdir(parents=True)
    (root / "A" / "song.flac").write_bytes(b"audio-bytes")
    return root


def test_local_storage_links_the_library_file_into_the_workspace(tmp_path, stocked):
    local = _local(stocked)
    workspace = tmp_path / "work" / "store"
    workspace.mkdir(parents=True)

    result = local.stream("A/song.flac", str(workspace), session=None)

    linked = workspace / result.file
    assert linked.read_bytes() == b"audio-bytes"
    assert os.stat(linked).st_ino == os.stat(stocked / "A" / "song.flac").st_ino  # a hard link, not a copy
    shutil.rmtree(workspace)
    assert (stocked / "A" / "song.flac").exists()  # the library is untouched


def test_local_storage_uses_its_own_root_setting(tmp_path):
    root = tmp_path / "library"
    local = _local(root)
    workspace = tmp_path / "work" / "fakedl"
    workspace.mkdir(parents=True)
    (workspace / "song.flac").write_bytes(b"audio")

    stored = local.store(WorkspaceFile(workspace=str(workspace), name="song.flac"), "A/B/Song.flac", session=None)

    assert stored == "A/B/Song.flac"
    assert (root / "A" / "B" / "Song.flac").read_bytes() == b"audio"


@pytest.mark.parametrize("relative", ["../outside.mp3", "/etc/passwd", "A/../../outside.mp3", "."])
def test_local_storage_refuses_paths_outside_its_folder(tmp_path, relative):
    storage = _local(tmp_path / "lib")

    with pytest.raises(ValueError):
        storage.exists(relative, AuthSession.anonymous())


def test_local_storage_accepts_paths_inside_its_folder(tmp_path):
    (tmp_path / "lib" / "A").mkdir(parents=True)
    (tmp_path / "lib" / "A" / "t.mp3").write_bytes(b"x")
    storage = _local(tmp_path / "lib")

    assert storage.exists("A/./t.mp3", AuthSession.anonymous())


# ------------------------------------------------------------------
# Added here
# ------------------------------------------------------------------


def _across_mounts(monkeypatch):
    """The library folder and the workspace are different mounts in the sandbox: a rename or a hard link between them
    fails with EXDEV."""
    def refuse(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", refuse)
    monkeypatch.setattr(os, "link", refuse)


def test_a_file_is_stored_across_mounts_by_copy_and_leaves_the_workspace_empty(tmp_path, monkeypatch):
    _across_mounts(monkeypatch)
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "song.flac").write_bytes(b"audio")

    _local(tmp_path / "library").store(WorkspaceFile(workspace=str(workspace), name="song.flac"), "A/Song.flac", session=None)

    assert (tmp_path / "library" / "A" / "Song.flac").read_bytes() == b"audio"
    assert not (workspace / "song.flac").exists()  # moved, as when it is one volume


def test_a_file_is_streamed_across_mounts_by_copy(tmp_path, stocked, monkeypatch):
    _across_mounts(monkeypatch)
    workspace = tmp_path / "work"
    workspace.mkdir()

    result = _local(stocked).stream("A/song.flac", str(workspace), session=None)

    assert (workspace / result.file).read_bytes() == b"audio-bytes"
    assert os.stat(workspace / result.file).st_ino != os.stat(stocked / "A" / "song.flac").st_ino
    assert (result.media_type, result.filename) == ("audio/flac", "song.flac")


@pytest.mark.parametrize("name, media_type", [
    ("a.mp3", "audio/mpeg"), ("a.flac", "audio/flac"), ("a.ogg", "audio/ogg"), ("a.m4a", "audio/mp4"),
    ("a.wav", "audio/wav"), ("a.opus", "audio/opus"), ("a.FLAC", "audio/flac"), ("a.xyz", "application/octet-stream"),
])
def test_the_media_type_comes_from_the_extension(tmp_path, name, media_type):
    root = tmp_path / "library"
    root.mkdir()
    (root / name).write_bytes(b"x")
    workspace = tmp_path / "work"
    workspace.mkdir()

    assert _local(root).stream(name, str(workspace), session=None).media_type == media_type


def test_exists_size_and_delete(tmp_path, stocked):
    local = _local(stocked)

    assert local.exists("A/song.flac", None) and not local.exists("A/none.flac", None)
    assert local.size("A/song.flac", None) == len(b"audio-bytes") and local.size("A/none.flac", None) is None
    assert local.delete("A/none.flac", None) is False
    assert local.delete("A/song.flac", None) is True and not (stocked / "A" / "song.flac").exists()


def test_the_connection_test_says_what_is_wrong_with_the_folder(tmp_path):
    assert _local(tmp_path).test_connection(None) is None
    assert "does not exist" in _local(tmp_path / "missing").test_connection(None)


def test_the_default_folder_is_the_one_of_the_setting_default():
    schema = {setting.key: setting for setting in _local("/x").config_schema()}

    assert schema["root_dir"].default == "/downloads" and schema["root_dir"].required is False
    assert LocalStorageProvider(StaticProviderContext())._root() == Path("/downloads")


def test_the_manifest_is_a_storage_provider_without_seek_by_offset_and_with_listing():
    from musizerr_sdk.manifest import build

    manifest = build(_local("/x"))

    assert (manifest.name, manifest.label, manifest.roles) == ("local", "Local Storage", ("storage",))
    assert manifest.declarations["supports_stream_offset"] is False
    assert {"store", "exists", "stream", "delete", "size", "list_files"} <= set(manifest.implements)
