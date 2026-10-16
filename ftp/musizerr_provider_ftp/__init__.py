"""FTP storage: the library on an FTP server (passive mode), streamed as it arrives and listed with MLSD or NLST."""
from __future__ import annotations

import ftplib
import logging
from pathlib import Path, PurePosixPath
from typing import Iterator

from musizerr_sdk import net
from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizerr_sdk.interfaces import PROVIDER_API_VERSION, StorageProvider, StorageStreamResult, WorkspaceFile
from musizerr_sdk.permissions import Permissions, TcpAccess
from musizerr_sdk.settings import ProviderSetting

logger = logging.getLogger(__name__)

# A sandbox has no network of its own: ftplib opens its sockets (the control connection and every passive data
# connection) with socket.create_connection, which from here on goes through the gateway's egress proxy. Outside
# a sandbox (the tests, a host that runs it in process) nothing changes: it is socket.create_connection itself.
net.route_module(ftplib)


class FTPStorageProvider(StorageProvider):
    name = "ftp"
    label = "FTP Storage"
    provider_api_version = PROVIDER_API_VERSION
    # Raw TCP to the host the user sets in `host`, on ANY port: the control connection is on the `port` setting
    # (21 by default) but in passive mode (the only one a sandbox allows) the server picks the port of every data
    # connection, so a fixed range cannot be declared. Private addresses are fine: the user typed the host.
    permissions = Permissions(tcp_from_settings=(TcpAccess("host", "any"),))
    # The storage of the instance: one login, managed by an admin.
    auth = AuthDefinition(flow=AuthFlow.CREDENTIALS, account_model=AccountModel.SYSTEM)
    supports_stream_offset = True

    def config_schema(self) -> list[ProviderSetting]:
        return [
            ProviderSetting(key="host", type="string", default=None, label="Host", required=True),
            ProviderSetting(key="port", type="string", default=None, label="Port",
                            required=False, description="Default: 21"),
            ProviderSetting(key="remote_base_path", type="string", default=None,
                            label="Remote base path", required=True,
                            description="Percorso remoto dove salvare i file (es. /music)"),
        ]

    def validate_config(self, config: dict) -> bool:
        return bool(config.get("host") and config.get("remote_base_path"))

    def test_connection(self, session: AuthSession) -> str | None:
        try:
            config = self._get_config()
            ftp = self._connect(session, config)
            ftp.cwd(config.get("remote_base_path", "/"))
            ftp.quit()
            return None
        except Exception as e:
            return str(e)

    # ------------------------------------------------------------------

    def _connect(self, session: AuthSession, config: dict | None = None, *, listening: bool = False) -> ftplib.FTP:
        if config is None:
            config = self._get_config()
        host = config.get("host", "localhost")
        port = int(config.get("port") or 21)
        ftp = ftplib.FTP()
        # A short timeout to connect: an unreachable server answers "timed
        # out" at once. The transfers that follow (control commands and the
        # data channel, which ftplib opens with ``ftp.timeout``) get the
        # longer per-file timeout. Someone ``listening`` waits for the first
        # byte: every step up to it (connect, login, opening the transfer)
        # gets the stream start timeout, and ``stream`` gives the transfer
        # the per-file one once it has started.
        if listening:
            ftp.connect(host, port, timeout=self.context.stream_start_timeout)
            ftp.timeout = self.context.stream_start_timeout
        else:
            ftp.connect(host, port, timeout=self.context.connect_timeout)
            _set_timeout(ftp, self.context.download_timeout)
        ftp.login(
            user=session.extra.get("username", ""),
            passwd=session.extra.get("password", ""),
        )
        return ftp

    def _remote_path(self, relative_path: str, config: dict) -> str:
        base = config.get("remote_base_path", "/")
        return str(PurePosixPath(base) / relative_path)

    def _get_config(self) -> dict:
        return dict(self.context.settings())

    def _mkdirs_remote(self, ftp: ftplib.FTP, remote_dir: str) -> None:
        """Crea ricorsivamente le directory remote se non esistono."""
        parts = PurePosixPath(remote_dir).parts
        current = ""
        for part in parts:
            current = str(PurePosixPath(current) / part) if current else part
            if not current.startswith("/"):
                current = "/" + current
            try:
                ftp.cwd(current)
            except ftplib.error_perm:
                ftp.mkd(current)
                ftp.cwd(current)

    def store(self, source: WorkspaceFile, relative_dest: str, session: AuthSession) -> str:
        config = self._get_config()
        remote = self._remote_path(relative_dest, config)
        remote_dir = str(PurePosixPath(remote).parent)

        ftp = self._connect(session, config)
        try:
            self._mkdirs_remote(ftp, remote_dir)
            with open(source.path, "rb") as f:
                ftp.storbinary(f"STOR {remote}", f)
            logger.debug("FTP stored: %s -> %s", source.name, remote)
        finally:
            ftp.quit()

        return relative_dest

    def exists(self, relative_path: str, session: AuthSession) -> bool:
        config = self._get_config()
        remote = self._remote_path(relative_path, config)
        ftp = self._connect(session, config)
        try:
            ftp.voidcmd("TYPE I")  # a server may refuse SIZE in ASCII mode (RFC 3659), as the other calls know
            ftp.size(remote)
            return True
        except ftplib.error_perm:
            return False
        finally:
            ftp.quit()

    def size(self, relative_path: str, session: AuthSession) -> int | None:
        config = self._get_config()
        remote = self._remote_path(relative_path, config)
        ftp = self._connect(session, config)
        try:
            ftp.voidcmd("TYPE I")  # SIZE richiede modalità binaria
            return ftp.size(remote)
        except (ftplib.error_perm, ftplib.error_reply, OSError):
            return None
        finally:
            ftp.quit()

    def stream(
        self, relative_path: str, workspace: str, session: AuthSession, offset: int = 0,
    ) -> StorageStreamResult:
        """The file as it arrives from the server, from ``offset`` (FTP
        ``REST``): the listener hears the first bytes at once instead of
        after the whole transfer. A file the server does not have fails here,
        before any byte."""
        config = self._get_config()
        remote = self._remote_path(relative_path, config)
        name = Path(relative_path).name

        ftp = self._connect(session, config, listening=True)
        try:
            ftp.voidcmd("TYPE I")  # SIZE e REST richiedono la modalità binaria
            try:
                size = ftp.size(remote)
            except (ftplib.error_perm, ftplib.error_reply):
                size = None
            if size is not None and offset >= size > 0:
                # Past the end: nothing to send (the host answers 416).
                ftp.close()
                return StorageStreamResult(
                    chunks=iter(()), media_type=_guess_media_type(Path(name)), filename=name, size=size, offset=offset,
                )
            connection = ftp.transfercmd(f"RETR {remote}", rest=offset or None)
        except BaseException:
            ftp.close()
            raise
        # The bytes are flowing: from here on a slow block is the transfer's.
        _set_timeout(ftp, self.context.download_timeout)
        connection.settimeout(self.context.download_timeout)

        return StorageStreamResult(
            chunks=_transfer(ftp, connection),
            media_type=_guess_media_type(Path(name)),
            filename=name,
            size=size,
            offset=offset,
        )

    def delete(self, relative_path: str, session: AuthSession) -> bool:
        config = self._get_config()
        remote = self._remote_path(relative_path, config)
        ftp = self._connect(session, config)
        try:
            ftp.delete(remote)
            logger.debug("FTP deleted: %s", remote)
            return True
        except ftplib.error_perm:
            return False
        finally:
            ftp.quit()

    def list_files(self, prefix: str, session: AuthSession) -> Iterator[str]:
        # Elenco completo prima di restituirlo: chi lo consuma può metterci
        # molto per file, e la connessione di controllo resterebbe inattiva.
        config = self._get_config()
        ftp = self._connect(session, config)
        try:
            ftp.cwd(config.get("remote_base_path", "/"))
            paths = [str(p) for p in _RemoteWalk(ftp).files(PurePosixPath(prefix))]
        finally:
            ftp.quit()
        return iter(paths)


_CHUNK_BYTES = 64 * 1024


def _set_timeout(ftp: ftplib.FTP, seconds: int) -> None:
    """The timeout of the control channel and of the data channels ftplib
    opens from now on."""
    ftp.timeout = seconds
    if ftp.sock is not None:
        ftp.sock.settimeout(seconds)


def _transfer(ftp: ftplib.FTP, connection) -> Iterator[bytes]:
    """The blocks of one RETR. Read to the end, the server's reply closes
    the transfer and the session; a listener who leaves early (the host
    closes this generator) drops both at once, without waiting for a reply
    to the aborted transfer."""
    complete = False
    try:
        while block := connection.recv(_CHUNK_BYTES):
            yield block
        complete = True
    finally:
        connection.close()
        if complete:
            try:
                ftp.voidresp()
                ftp.quit()
            except ftplib.all_errors:
                ftp.close()
        else:
            ftp.close()


class _RemoteWalk:
    """Visita ricorsiva di una cartella remota, relativa alla cartella
    corrente della connessione. Usa MLSD; sui server che non lo supportano
    ricade su NLST, riconoscendo le cartelle perché vi si può entrare."""

    def __init__(self, ftp: ftplib.FTP) -> None:
        self._ftp = ftp
        self._mlsd = True

    def files(self, directory: PurePosixPath) -> Iterator[PurePosixPath]:
        for name, is_dir in self._entries(directory):
            path = directory / name
            if is_dir:
                yield from self.files(path)
            else:
                yield path

    def _entries(self, directory: PurePosixPath) -> list[tuple[str, bool]]:
        if self._mlsd:
            try:
                return [
                    (name, facts["type"] == "dir")
                    for name, facts in self._ftp.mlsd(str(directory), facts=["type"])
                    if facts.get("type") in ("file", "dir")
                ]
            except ftplib.error_perm as exc:
                if not str(exc).startswith(("500", "502")):  # comando sconosciuto
                    logger.warning("FTP folder %s not listed: %s", directory, exc)
                    return []
                self._mlsd = False
        return self._nlst_entries(directory)

    def _nlst_entries(self, directory: PurePosixPath) -> list[tuple[str, bool]]:
        try:
            names = [PurePosixPath(entry).name for entry in self._ftp.nlst(str(directory))]
        except ftplib.error_perm:
            return []  # molti server rispondono 550 per una cartella vuota
        start = self._ftp.pwd()
        entries = []
        for name in names:
            if name in ("", ".", ".."):
                continue
            try:
                self._ftp.cwd(str(directory / name))
                is_dir = True
            except ftplib.error_perm:
                is_dir = False
            else:
                self._ftp.cwd(start)
            entries.append((name, is_dir))
        return entries


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
