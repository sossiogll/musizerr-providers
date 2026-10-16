"""Local storage: the library in a folder of the machine the gateway runs on."""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Iterator

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizerr_sdk.interfaces import PROVIDER_API_VERSION, StorageProvider, StorageStreamResult, WorkspaceFile
from musizerr_sdk.permissions import FilesystemAccess, Permissions
from musizerr_sdk.settings import ProviderSetting

logger = logging.getLogger(__name__)

_DEFAULT_ROOT = "/downloads"


class LocalStorageProvider(StorageProvider):
    name = "local"
    label = "Local Storage"
    provider_api_version = PROVIDER_API_VERSION
    # Read and write in the one folder the user sets in `root_dir` (the library), and nowhere else: the sandbox binds
    # that folder, at the same path, when the gateway grants it. No network.
    permissions = Permissions(filesystem_from_settings=(FilesystemAccess("root_dir", "rw"),))
    # A folder of the host: nothing to log in to.
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def config_schema(self) -> list[ProviderSetting]:
        return [
            ProviderSetting(
                key="root_dir", type="string", default=_DEFAULT_ROOT,
                label="Library folder",
                required=False,
                description="Folder where the library files are stored.",
            ),
        ]

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session: AuthSession) -> str | None:
        d = self._root()
        if not d.is_dir():
            return f"Library folder does not exist: {d}"
        if not os.access(d, os.W_OK):
            return f"Library folder is not writable: {d}"
        return None

    # ------------------------------------------------------------------

    def _root(self) -> Path:
        return Path(self.context.setting("root_dir") or _DEFAULT_ROOT)

    def _full(self, relative_path: str) -> Path:
        """``relative_path`` dentro la cartella della libreria. Il controllo è
        lessicale (non segue i link simbolici, che la libreria può usare di
        proposito): rifiuta solo i path assoluti o che risalgono fuori."""
        root = Path(os.path.normpath(self._root()))
        full = Path(os.path.normpath(root / relative_path))
        if full == root or not full.is_relative_to(root):
            raise ValueError(f"'{relative_path}' is outside the library folder")
        return full

    def store(self, source: WorkspaceFile, relative_dest: str, session: AuthSession) -> str:
        dest = self._full(relative_dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Rename istantaneo quando la cartella di lavoro sta sullo stesso
        # volume della libreria; altrimenti copia.
        shutil.move(source.path, str(dest))
        logger.debug("Stored file: %s -> %s", source.name, dest)
        return relative_dest

    def exists(self, relative_path: str, session: AuthSession) -> bool:
        return (self._full(relative_path)).exists()

    def stream(self, relative_path: str, workspace: str, session: AuthSession) -> StorageStreamResult:
        full = self._full(relative_path)
        link = Path(workspace) / full.name
        # Un hard link costa O(1) sullo stesso volume; altrimenti copia.
        try:
            os.link(full, link)
        except OSError:
            shutil.copy2(full, link)
        return StorageStreamResult(
            file=link.name,
            media_type=_guess_media_type(full),
            filename=full.name,
        )

    def delete(self, relative_path: str, session: AuthSession) -> bool:
        full = self._full(relative_path)
        if full.exists():
            full.unlink()
            logger.debug("Deleted file: %s", full)
            return True
        return False

    def size(self, relative_path: str, session: AuthSession) -> int | None:
        full = self._full(relative_path)
        try:
            return full.stat().st_size
        except OSError:
            return None

    def list_files(self, prefix: str, session: AuthSession) -> Iterator[str]:
        root = Path(os.path.normpath(self._root()))
        base = self._full(prefix) if prefix else root
        if not base.is_dir():
            raise FileNotFoundError(f"Library folder does not exist: {base}")
        return _walk(root, base)


def _walk(root: Path, base: Path) -> Iterator[str]:
    # I link simbolici a file sono elencati; quelli a cartelle non vengono
    # percorsi, così un link circolare non blocca la visita.
    for dirpath, _dirnames, filenames in os.walk(base):
        for filename in filenames:
            yield (Path(dirpath) / filename).relative_to(root).as_posix()


def _guess_media_type(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
        ".opus": "audio/opus",
    }.get(ext, "application/octet-stream")
