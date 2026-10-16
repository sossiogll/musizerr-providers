"""
The FTP storage against a REAL FTP server (pyftpdlib, in this process, on the loopback, passive mode), and again with
every socket going through a CONNECT proxy like the one of the gateway.

The second half is the evidence for the permission the provider declares: the control connection and each passive
data connection are separate TCP connections, the data one on a port the SERVER chose, so the sandbox must be allowed
raw TCP to the host on any port.
"""
from __future__ import annotations

import socket
import socketserver
import threading

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from musizerr_provider_ftp import FTPStorageProvider
from musizerr_sdk import net
from musizerr_sdk.auth import AuthFlow, AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.interfaces import WorkspaceFile

SESSION = AuthSession(flow=AuthFlow.CREDENTIALS, extra={"username": "user", "password": "synthetic-password"})
AUDIO = bytes(range(256)) * 512  # 128 KiB


def _serve(tmp_path, *, strict_size: bool):
    """A server with its home in ``tmp_path/remote``: yields ``(port, remote_dir)``. A server answers SIZE in ASCII mode
    (the default TYPE of a session) or refuses it, as the strictest do (RFC 3659 leaves it open)."""
    remote = tmp_path / "remote"
    (remote / "music").mkdir(parents=True)
    authorizer = DummyAuthorizer()
    authorizer.add_user("user", "synthetic-password", str(remote), perm="elradfmwMT")

    class Handler(FTPHandler):
        def ftp_SIZE(self, path):
            if strict_size:
                return super().ftp_SIZE(path)
            kept, self._current_type = self._current_type, "i"
            try:
                return super().ftp_SIZE(path)
            finally:
                self._current_type = kept

    Handler.authorizer = authorizer
    Handler.passive_ports = range(41000, 41100)  # the server picks one of these for each data connection
    Handler.masquerade_address = None
    ftpd = FTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: ftpd.serve_forever(timeout=0.05, handle_exit=False), daemon=True)
    thread.start()
    try:
        yield ftpd.address[1], remote
    finally:
        ftpd.close_all()
        thread.join(timeout=5)


@pytest.fixture
def server(tmp_path):
    yield from _serve(tmp_path, strict_size=False)


@pytest.fixture
def strict_server(tmp_path):
    yield from _serve(tmp_path, strict_size=True)


def _provider(port: int) -> FTPStorageProvider:
    return FTPStorageProvider(StaticProviderContext(
        settings={"host": "127.0.0.1", "port": str(port), "remote_base_path": "/music"}, provider_name="ftp",
    ))


def _workspace_file(directory, name: str, data: bytes) -> WorkspaceFile:
    (directory / name).write_bytes(data)
    return WorkspaceFile(workspace=str(directory), name=name)


def _exercise(provider: FTPStorageProvider, tmp_path, remote) -> None:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)

    assert provider.test_connection(SESSION) is None
    assert provider.exists("Artist/Album/01 song.flac", SESSION) is False

    stored = provider.store(_workspace_file(work, "song.flac", AUDIO), "Artist/Album/01 song.flac", SESSION)

    assert stored == "Artist/Album/01 song.flac"
    assert (remote / "music" / "Artist" / "Album" / "01 song.flac").read_bytes() == AUDIO
    assert provider.exists("Artist/Album/01 song.flac", SESSION) is True
    assert provider.size("Artist/Album/01 song.flac", SESSION) == len(AUDIO)

    whole = provider.stream("Artist/Album/01 song.flac", str(work), SESSION)
    assert (whole.size, whole.media_type, whole.filename, whole.offset) == (len(AUDIO), "audio/flac", "01 song.flac", 0)
    assert b"".join(whole.chunks) == AUDIO

    seek = provider.stream("Artist/Album/01 song.flac", str(work), SESSION, offset=100_000)
    assert seek.offset == 100_000 and b"".join(seek.chunks) == AUDIO[100_000:]

    past_the_end = provider.stream("Artist/Album/01 song.flac", str(work), SESSION, offset=len(AUDIO))
    assert list(past_the_end.chunks) == [] and past_the_end.size == len(AUDIO)

    assert sorted(provider.list_files("", SESSION)) == ["Artist/Album/01 song.flac"]
    assert provider.delete("Artist/Album/01 song.flac", SESSION) is True
    assert provider.exists("Artist/Album/01 song.flac", SESSION) is False
    assert provider.delete("Artist/Album/01 song.flac", SESSION) is False


def test_store_exists_stream_list_and_delete_against_a_real_server(server, tmp_path):
    port, remote = server

    _exercise(_provider(port), tmp_path, remote)


def test_a_wrong_password_is_reported_by_the_connection_test(server):
    port, _ = server
    bad = AuthSession(flow=AuthFlow.CREDENTIALS, extra={"username": "user", "password": "wrong"})

    assert "530" in _provider(port).test_connection(bad)


def test_a_server_that_is_not_there_is_reported_not_raised():
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()

    assert _provider(port).test_connection(SESSION)  # some text: the connection was refused


# ---------------------------------------------------------------------------
# Through a CONNECT proxy, as in the sandbox
# ---------------------------------------------------------------------------


class _ConnectProxy(socketserver.ThreadingTCPServer):
    """What the gateway's egress proxy does for raw TCP, reduced to its essentials: ``CONNECT host:port``, answer 200,
    splice. It records the destinations."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ConnectHandler)
        self.destinations: list[tuple[str, int]] = []


class _ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            byte = self.request.recv(1)
            if not byte:
                return
            head += byte
        method, target, _ = head.split(b"\r\n", 1)[0].decode().split(" ", 2)
        assert method == "CONNECT"
        host, _, port = target.rpartition(":")
        self.server.destinations.append((host, int(port)))
        upstream = socket.create_connection((host, int(port)))
        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        def pump(source: socket.socket, sink: socket.socket) -> None:
            try:
                while data := source.recv(65536):
                    sink.sendall(data)
            except OSError:
                pass
            finally:
                for end in (source, sink):
                    try:
                        end.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        thread = threading.Thread(target=pump, args=(upstream, self.request), daemon=True)
        thread.start()
        pump(self.request, upstream)
        thread.join(timeout=5)
        upstream.close()


@pytest.fixture
def proxy():
    connect = _ConnectProxy()
    thread = threading.Thread(target=connect.serve_forever, daemon=True)
    thread.start()
    net.use_proxy(connect.server_address)
    try:
        yield connect
    finally:
        net.use_proxy(None)
        connect.shutdown()
        connect.server_close()


def test_every_connection_goes_through_the_proxy_and_the_data_ones_are_on_other_ports(server, proxy, tmp_path):
    port, remote = server

    _exercise(_provider(port), tmp_path, remote)

    ports = {p for host, p in proxy.destinations if host == "127.0.0.1"}
    assert port in ports                                       # the control connection
    data_ports = ports - {port}
    assert data_ports and all(41000 <= p < 41100 for p in data_ports)   # the passive ones, picked by the server
    assert {host for host, _ in proxy.destinations} == {"127.0.0.1"}    # always the host the user set


def test_exists_on_a_server_that_refuses_size_in_ascii_mode(strict_server):
    """Stock pyftpdlib refuses SIZE in ASCII mode (RFC 3659 allows it): `exists` asks for binary mode first, so a file
    that is there is found, and one that is not is not."""
    port, remote = strict_server
    (remote / "music" / "song.flac").write_bytes(b"x")

    assert _provider(port).exists("song.flac", SESSION) is True
    assert _provider(port).exists("nothing.flac", SESSION) is False


def test_exists_on_a_server_that_answers_size_in_ascii_mode(server):
    port, remote = server
    (remote / "music" / "song.flac").write_bytes(b"x")

    assert _provider(port).exists("song.flac", SESSION) is True
    assert _provider(port).exists("nothing.flac", SESSION) is False
