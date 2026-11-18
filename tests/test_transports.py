"""The default transports against local servers (SPEC.md §13, §15): HTTPS with a certificate of
this run (made here, never stored), and WebSocket. Never the network."""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import http
import http.server
import ipaddress
import secrets
import socket
import ssl
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import websockets.asyncio.server
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from sattleme_checkout._crypto import Secret
from sattleme_checkout._http import DefaultTransport, Http, _Connection
from sattleme_checkout._relay import RelayPool, default_connect
from sattleme_checkout.errors import NetworkError

MIB = 1 << 20


def _certificate(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


_HEAD_MAX = 10 * MIB


def _chunked(chunks: int, extension: int) -> bytes:
    """An answer of `chunks` chunks of one byte, each size line with an extension of
    `extension` bytes (`http.client` reads a line of 64 KiB at most): its size lines alone make
    a head of about chunks × extension bytes, its body `chunks` bytes."""
    line = b"1;x=" + b"y" * extension + b"\r\n"
    return (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        + (line + b"{\r\n") * chunks
        + b"0\r\n\r\n"
    )


def _continues(count: int) -> bytes:
    """`count` interim answers (100 Continue), each with ten headers of 60 000 bytes, then an
    answer: a head of about count × 600 000 bytes."""
    interim = (
        b"HTTP/1.1 100 Continue\r\n"
        + b"".join(b"X-Pad-%d: %s\r\n" % (i, b"z" * 60_000) for i in range(10))
        + b"\r\n"
    )
    return interim * count + b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"


def _trailed(trailers: bytes) -> bytes:
    """A chunked answer of `{}` followed by `trailers` (each line ending CRLF)."""
    return (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        b"2\r\n{}\r\n0\r\n" + trailers + b"\r\n"
    )


_ELEVEN_MIB = 11 * MIB

# Made when asked: some 10 MB each.
_HEADS: dict[str, Callable[[], bytes]] = {
    # One header line larger than the bound.
    "line": lambda: b"HTTP/1.1 200 OK\r\nX-Pad: " + b"z" * (_HEAD_MAX + 1) + b"\r\n\r\n{}",
    # Trailers, 11 MiB in all: 181 lines of 64 000 bytes (the `http.client` of some versions,
    # 3.12.3 and 3.14.5 here, bounds each line, not their count), or one line.
    "trailers": lambda: _trailed(
        b"".join(b"X-T-%d: %s\r\n" % (i, b"t" * 64_000) for i in range(181))
    ),
    "trailer-line": lambda: _trailed(b"X-T: " + b"t" * _ELEVEN_MIB + b"\r\n"),
    # 11 MiB of zeros before a chunk size, and a chunk extension of 11 MiB: `http.client` refuses
    # a size line over 64 KiB.
    "zeros": lambda: (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        + b"0" * _ELEVEN_MIB
        + b"2\r\n{}\r\n0\r\n\r\n"
    ),
    "extension": lambda: (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        b"2;" + b"x" * _ELEVEN_MIB + b"\r\n{}\r\n0\r\n\r\n"
    ),
    # Size lines of a chunked body, 11 MiB in all; then 9 MiB, within the bound.
    "chunks": lambda: _chunked(176, 65_000),
    "chunks-within": lambda: _chunked(144, 65_000),
    # Interim answers, 12 MB of head in all; then 9 MB, within the bound.
    "continues": lambda: _continues(20),
    "continues-within": lambda: _continues(15),
}


_CHUNKED = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"

# Answers that never end: a head, then a line again and again.
_ENDLESS: dict[str, tuple[bytes, bytes]] = {
    "chunks": (_CHUNKED, b"1;x=" + b"y" * 65_000 + b"\r\n{\r\n"),
    "trailers": (_CHUNKED + b"2\r\n{}\r\n0\r\n", b"X-T: " + b"t" * 64_000 + b"\r\n"),
}


class _Handler(http.server.BaseHTTPRequestHandler):
    asked: list[str] = []  # noqa: RUF012 - shared by the test's requests
    headers_asked: list[dict[str, str]] = []  # noqa: RUF012 - shared by the test's requests
    # When the client stopped reading a dripped answer (it shut its socket).
    dropped: threading.Event = threading.Event()

    def log_message(self, *_args: Any) -> None:
        return

    def do_GET(self) -> None:
        self.asked.append(self.path)
        self.headers_asked.append(dict(self.headers.items()))
        if self.path == "/json":
            self._send(200, b'{"ok": true}')
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/exactly":
            self._send(200, b" " * MIB)
        elif self.path == "/big":
            self._send(200, b" " * (MIB + 1))
        elif self.path == "/slow":
            time.sleep(3)
            self._send(200, b"{}")
        elif self.path == "/drip":
            # HTTP/1.0, no length: the answer ends the connection, read until it closes; one
            # byte each 0.1 s, within any socket timeout.
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(50):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                self.dropped.set()
        elif self.path.startswith("/head/"):
            self._raw(_HEADS[self.path.removeprefix("/head/")]())
        elif self.path.startswith("/endless/"):
            self._endless(*_ENDLESS[self.path.removeprefix("/endless/")])
        elif self.path == "/stall":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"{")
            self.wfile.flush()
            time.sleep(3)
        else:
            self._send(404, b"{}")

    def _endless(self, head: bytes, line: bytes) -> None:
        """An answer of `head`, then of `line` again and again, until the client closes its
        connection (200 MiB at most): what the sockets' buffers hold never ends it."""
        try:
            self.wfile.write(head)
            for _ in range(200 * MIB // len(line)):
                self.wfile.write(line)
            self.wfile.flush()
        except OSError:
            self.dropped.set()
        self.close_connection = True

    def _raw(self, answer: bytes) -> None:
        """An answer written as it is; the client may stop reading it (the head too large)."""
        try:
            self.wfile.write(answer)
            self.wfile.flush()
        except OSError:
            self.dropped.set()
        self.close_connection = True

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def https(tmp_path: Path) -> Iterator[tuple[str, ssl.SSLContext]]:
    cert_path, key_path = _certificate(tmp_path)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    serving = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    serving.load_cert_chain(cert_path, key_path)
    server.socket = serving.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    trusting = ssl.create_default_context(cafile=str(cert_path))
    _Handler.asked = []
    _Handler.headers_asked = []
    _Handler.dropped = threading.Event()
    try:
        # The address itself, not `localhost` (which Windows tries first over IPv6).
        yield f"https://127.0.0.1:{server.server_address[1]}", trusting
    finally:
        server.shutdown()
        server.server_close()


async def test_an_answer(https: tuple[str, ssl.SSLContext]) -> None:
    base, context = https
    status, body = await Http(DefaultTransport(context), 2000).get_json(f"{base}/json")
    assert (status, body) == (200, {"ok": True})
    exactly = await Http(DefaultTransport(context), 2000).get(f"{base}/exactly")
    assert len(exactly.body) == MIB


@pytest.mark.parametrize(
    "answer", ["line", "chunks", "continues", "trailers", "trailer-line", "zeros", "extension"]
)
async def test_an_answers_head_larger_than_10_mib_is_no_answer(
    https: tuple[str, ssl.SSLContext], answer: str
) -> None:
    """SPEC.md §15, Head: what the client holds of its status line, its headers, and a chunked
    body's size lines and trailers is bounded too, 10 MiB at most in all: a larger head is no
    answer (`network`). One header line larger than that; a chunked body of 176 bytes whose size
    lines make 11 MiB; interim answers (100 Continue) whose headers make 12 MB (`http.client`
    bounds each line and the headers of one answer, not their count); trailers of 11 MiB, in 181
    lines or in one; 11 MiB of zeros before a chunk size, and a chunk extension of 11 MiB (each
    line refused at 64 KiB, never read through)."""
    base, context = https
    with pytest.raises(NetworkError):
        await Http(DefaultTransport(context), 30_000).get(f"{base}/head/{answer}")


@pytest.mark.parametrize("answer", ["chunks", "trailers"])
async def test_an_answer_refused_while_its_body_is_read_closes_its_connection(
    https: tuple[str, ssl.SSLContext], answer: str
) -> None:
    """Nothing a call started runs on once it returned (SPEC.md §13), its connections included.
    An answer refused while its body is read (its size lines past the bound, too many trailers)
    closes its connection at once, though its error is held (its traceback holds the frame that
    read it, as an error tracker holds the error): the server, which writes on without end, sees
    it closed. It stayed open, the server writing into it, for as long as the error was held."""
    base, context = https
    with pytest.raises(NetworkError) as raised:
        await Http(DefaultTransport(context), 30_000).get(f"{base}/endless/{answer}")
    assert await asyncio.to_thread(_Handler.dropped.wait, 5)
    assert raised.value.__cause__ is not None


@pytest.mark.parametrize(
    ("answer", "body"),
    [("chunks-within", b"{" * 144), ("continues-within", b"{}")],
    ids=["chunks", "continues"],
)
async def test_an_answers_head_within_10_mib_is_read(
    https: tuple[str, ssl.SSLContext], answer: str, body: bytes
) -> None:
    base, context = https
    response = await Http(DefaultTransport(context), 30_000).get(f"{base}/head/{answer}")
    assert (response.status, response.body) == (200, body)


async def test_every_request_names_the_user_agent(https: tuple[str, ssl.SSLContext]) -> None:
    """SPEC.md §15: `User-Agent: sattleme-Checkout/1` and `Accept: application/json`,
    whatever other header a request carries (CoinGecko's key)."""
    base, context = https
    client = Http(DefaultTransport(context), 2000)
    await client.get(base + "/json")
    # A secret's header, as CoinGecko's key is sent: revealed at the transport's call.
    await client.get(base + "/json", secret_headers={"x-cg-demo-api-key": Secret(b"CG-demo")})
    # Never replaced by a caller's header.
    await client.get(base + "/json", {"User-Agent": "other", "Accept": "text/html"})
    assert [(h["User-Agent"], h["Accept"]) for h in _Handler.headers_asked] == [
        ("sattleme-Checkout/1", "application/json")
    ] * 3
    assert _Handler.headers_asked[1]["x-cg-demo-api-key"] == "CG-demo"


def _frames_of(error: BaseException) -> Iterator[Any]:
    """Every frame along `error`'s traceback, its cause's and its context's."""
    seen: set[int] = set()
    todo: list[BaseException | None] = [error]
    while todo:
        current = todo.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for frame, _line in traceback.walk_tb(current.__traceback__):
            yield frame
        todo += [current.__cause__, current.__context__]


async def test_a_connection_that_fails_leaves_no_frame_holding_a_secret_header() -> None:
    """A secret header (CoinGecko's key) is revealed in the headers made at the transport's
    call only, whose representation names them, never their values; and the default transport
    connects before it writes the request: a connection that fails (refused here) leaves no
    frame along the error's traceback, the standard library's included, holding the key, as an
    error tracker captures their local variables."""
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    key = "CG-a-key-of-this-test"
    # A timeout far above the refusal's time: Windows refuses a connection to a closed local port
    # only after its retries, about 2 s, and the timeout would end the request first.
    with pytest.raises(NetworkError) as raised:
        await Http(DefaultTransport(), 10_000).get(
            f"https://127.0.0.1:{port}/json",
            secret_headers={"x-cg-demo-api-key": Secret(key.encode())},
        )
    # Every frame but this test's own, which holds what it holds.
    frames = [f for f in _frames_of(raised.value) if f.f_code.co_filename != __file__]
    # The connection's own frames are there: the scan covers them.
    assert any(frame.f_code.co_name == "connect" for frame in frames)
    shown = [
        f"{frame.f_code.co_filename}:{frame.f_code.co_name}: {name}"
        for frame in frames
        for name, value in frame.f_locals.items()
        if key in repr(value)
    ]
    assert shown == []


async def test_a_redirect_is_never_followed(https: tuple[str, ssl.SSLContext]) -> None:
    base, context = https
    with pytest.raises(NetworkError, match="redirect"):
        await Http(DefaultTransport(context), 2000).get(f"{base}/redirect")
    assert _Handler.asked == ["/redirect"]


async def test_a_body_larger_than_1_mib(https: tuple[str, ssl.SSLContext]) -> None:
    base, context = https
    with pytest.raises(NetworkError, match="1 MiB"):
        await Http(DefaultTransport(context), 2000).get(f"{base}/big")


@pytest.mark.parametrize("path", ["/slow", "/stall"])
async def test_the_timeout_bounds_the_whole_request(
    https: tuple[str, ssl.SSLContext], path: str
) -> None:
    base, context = https
    started = time.monotonic()
    with pytest.raises(NetworkError, match="in time"):
        await Http(DefaultTransport(context), 500).get(f"{base}{path}")
    assert time.monotonic() - started < 1.5


async def test_a_request_cancelled_before_its_socket_never_goes_on(
    https: tuple[str, ssl.SSLContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled while its name resolves (no socket yet), the worker thread makes no connection
    afterwards: a host that answers late never holds the package's threads."""
    base, context = https
    resolving, release = threading.Event(), threading.Event()
    resolve = socket.getaddrinfo

    def slow(*args: Any, **kwargs: Any) -> Any:
        resolving.set()
        release.wait(5)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    task = asyncio.ensure_future(Http(DefaultTransport(context), 5000).get(f"{base}/json"))
    assert await asyncio.to_thread(resolving.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    # The thread resolves now: it would connect and ask within milliseconds.
    await asyncio.sleep(1)
    assert _Handler.asked == []


async def test_a_dripped_answer_that_ends_the_connection_ends_at_the_timeout(
    https: tuple[str, ssl.SSLContext],
) -> None:
    """An answer that ends the connection (HTTP/1.0, no length) is read after `http.client`
    cleared its socket: the timeout still shuts it, and the worker thread stops reading."""
    base, context = https
    started = time.monotonic()
    with pytest.raises(NetworkError, match="in time"):
        await Http(DefaultTransport(context), 500).get(f"{base}/drip")
    assert await asyncio.to_thread(_Handler.dropped.wait, 3)
    assert time.monotonic() - started < 2.5


def _stalled_tls_server() -> tuple[socket.socket, threading.Event]:
    """A TCP server that never answers a TLS handshake; the event: the client closed."""
    server = socket.create_server(("127.0.0.1", 0))
    closed = threading.Event()

    def serve() -> None:
        connection, _address = server.accept()
        with connection:
            while connection.recv(4096):
                pass
        closed.set()

    threading.Thread(target=serve, daemon=True).start()
    return server, closed


def test_abandoned_in_its_tls_handshake_a_connection_ends_at_once() -> None:
    server, closed = _stalled_tls_server()
    try:
        port = server.getsockname()[1]
        connection = _Connection("127.0.0.1", port, timeout=4, context=ssl.create_default_context())
        failed: list[BaseException] = []

        def connect() -> None:
            try:
                connection.connect()
            except Exception as error:  # noqa: BLE001 - what the test looks at
                failed.append(error)

        worker = threading.Thread(target=connect)
        started = time.monotonic()
        worker.start()
        time.sleep(0.3)
        connection.abandon()
        worker.join(3)
        assert not worker.is_alive()
        assert time.monotonic() - started < 2
        assert failed
        assert closed.wait(2)
        connection.close()
    finally:
        server.close()


async def test_an_address_of_a_family_the_system_lacks_is_skipped(
    https: tuple[str, ssl.SSLContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    """As `socket.create_connection`: a socket that cannot be made is the next address's turn."""
    base, context = https
    resolve = socket.getaddrinfo

    def with_unknown_family(*args: Any, **kwargs: Any) -> Any:
        return [(9999, socket.SOCK_STREAM, 0, "", ("::1", 0)), *resolve(*args, **kwargs)]

    monkeypatch.setattr(socket, "getaddrinfo", with_unknown_family)
    status, body = await Http(DefaultTransport(context), 2000).get_json(f"{base}/json")
    assert (status, body) == (200, {"ok": True})


def test_a_connection_abandoned_before_it_starts_resolves_no_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[object] = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: resolved.append(args))
    connection = _Connection("127.0.0.1", 1, timeout=2, context=ssl.create_default_context())
    connection.abandon()
    with pytest.raises(ConnectionAbortedError):
        connection.connect()
    assert resolved == []


def test_a_connection_sets_tcp_nodelay(https: tuple[str, ssl.SSLContext]) -> None:
    base, context = https
    port = int(base.rsplit(":", 1)[1])
    connection = _Connection("127.0.0.1", port, timeout=2, context=context)
    try:
        connection.connect()
        assert connection.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
    finally:
        connection.close()


async def test_an_untrusted_certificate(https: tuple[str, ssl.SSLContext]) -> None:
    base, _context = https
    with pytest.raises(NetworkError):
        await Http(DefaultTransport(), 2000).get(f"{base}/json")


async def test_https_only() -> None:
    with pytest.raises(NetworkError):
        await Http(DefaultTransport(), 2000).get("http://127.0.0.1:1/json")


@pytest.fixture
def plain_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """A server in clear HTTP on 127.0.0.1, which the names `umbrel.local` and `node.example`
    resolve to: a merchant's Bitcoin node on its network, and a host of the Internet."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.asked = []
    _Handler.headers_asked = []
    _Handler.dropped = threading.Event()
    resolve = socket.getaddrinfo

    def names(host: Any, *args: Any, **kwargs: Any) -> Any:
        return resolve(
            "127.0.0.1" if host in ("umbrel.local", "node.example") else host, *args, **kwargs
        )

    monkeypatch.setattr(socket, "getaddrinfo", names)
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


async def test_http_to_a_merchants_node_on_a_local_network(plain_http: int) -> None:
    """SPEC.md §15, §31: the node's URL allows http for a local host; the request is asked as
    every other (its User-Agent, no redirect followed)."""
    http_ = Http(DefaultTransport(), 2000)
    status, body = await http_.get_json(f"http://umbrel.local:{plain_http}/json")
    assert (status, body) == (200, {"ok": True})
    assert _Handler.headers_asked[0]["User-Agent"] == "sattleme-Checkout/1"
    with pytest.raises(NetworkError, match="redirect"):
        await http_.get(f"http://umbrel.local:{plain_http}/redirect")
    assert _Handler.asked == ["/json", "/redirect"]


@pytest.mark.parametrize(
    "url",
    [
        "http://node.example:{port}/json",
        "http://127.0.0.1:{port}/json",
        "http://user@umbrel.local:{port}/json",
        "ws://umbrel.local:{port}/json",
        "ftp://umbrel.local:{port}/json",
    ],
)
async def test_no_other_http_url_is_asked(plain_http: int, url: str) -> None:
    """Not a node URL of a local host: refused before any connection."""
    with pytest.raises(NetworkError):
        await Http(DefaultTransport(), 2000).get(url.format(port=plain_http))
    assert _Handler.asked == []


@pytest.mark.parametrize(("size", "taken"), [(MIB, True), (MIB + 1, False)])
async def test_a_websocket_message_is_1_mib_at_most(size: int, taken: bool) -> None:
    async def send(socket: websockets.asyncio.server.ServerConnection) -> None:
        await socket.send("x" * size)
        await asyncio.sleep(1)

    server = await websockets.asyncio.server.serve(send, "127.0.0.1", 0, max_size=None)
    try:
        port = server.sockets[0].getsockname()[1]
        connection: Any = await default_connect(f"ws://127.0.0.1:{port}")
        try:
            if taken:
                assert len(await connection.recv()) == size
            else:
                with pytest.raises(websockets.ConnectionClosedError):
                    await connection.recv()
        finally:
            await connection.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_the_answer_to_a_websocket_handshake_is_bounded() -> None:
    """SPEC.md §15, Head: a refusal of the opening handshake whose chunked body sends size lines
    of 8 000 bytes for one byte each, without end (`websockets` bounds each line and the body,
    not their count: 8 GB before the body's 1 MiB): the connection fails, having read some
    11 MiB, never more."""
    written = 0
    ended = asyncio.Event()

    async def refuse(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal written
        await reader.readuntil(b"\r\n\r\n")
        head = b"HTTP/1.1 403 Forbidden\r\nTransfer-Encoding: chunked\r\n\r\n"
        chunk = (b"1;x=" + b"y" * 8000 + b"\r\n{\r\n") * 128
        try:
            writer.write(head)
            while written < 200 * MIB:
                writer.write(chunk)
                await writer.drain()
                written += len(chunk)
        except OSError:
            pass
        finally:
            ended.set()
            writer.close()

    server = await asyncio.start_server(refuse, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        async with asyncio.timeout(60):
            with pytest.raises(websockets.InvalidHandshake):
                await default_connect(f"ws://127.0.0.1:{port}")
            await ended.wait()
        # What it read, and what the sockets' buffers held when it stopped reading.
        assert written < 24 * MIB
    finally:
        server.close()
        await server.wait_closed()


async def test_a_websocket_handshakes_answer_with_a_header_of_11_mib_is_no_answer() -> None:
    """SPEC.md §15, Head, and §13: what the client holds of the answer to a WebSocket's opening
    handshake is bounded as an HTTP answer's head. An answer whose one header is 11 MiB: the
    relay's connection fails (`websockets` refuses a line of more than 8 KiB), and a read of that
    relay alone is no answer (`network`)."""
    answered = asyncio.Event()

    async def huge(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        try:
            writer.write(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nX-Big: " + b"a" * _ELEVEN_MIB + b"\r\n\r\n"
            )
            await writer.drain()
        except OSError:
            pass
        finally:
            answered.set()
            writer.close()

    server = await asyncio.start_server(huge, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(websockets.InvalidHandshake):
            await default_connect(f"ws://127.0.0.1:{port}")

        async def connect(_url: str) -> Any:
            return await default_connect(f"ws://127.0.0.1:{port}")

        async with RelayPool(
            connect=connect, timeout_ms=10_000, random=secrets.token_bytes, now=lambda: 0
        ) as pool:
            with pytest.raises(NetworkError):
                await pool.read(["wss://a.example"], {"kinds": [1]}, lambda value: value)
        await answered.wait()
    finally:
        server.close()
        await server.wait_closed()


async def test_a_websocket_handshake_names_the_user_agent_and_offers_no_compression() -> None:
    """The opening handshake is an HTTP request (SPEC.md §15: its User-Agent). It offers no
    compression: a relay's few compressed bytes could unpack into many messages at once, all
    held before reading pauses, where a message of 1 MiB at most keeps what a relay sends in
    bounds; and sealed records, encrypted, do not compress."""
    asked: list[Any] = []

    def record(_connection: Any, request: Any) -> None:
        asked.append(request.headers)

    async def echo(socket: websockets.asyncio.server.ServerConnection) -> None:
        await socket.send("ok")

    # The server offers compression by default: only the client's offer could make it used.
    server = await websockets.asyncio.server.serve(echo, "127.0.0.1", 0, process_request=record)
    try:
        port = server.sockets[0].getsockname()[1]
        connection: Any = await default_connect(f"ws://127.0.0.1:{port}")
        try:
            assert await connection.recv() == "ok"
            assert connection.protocol.extensions == []
        finally:
            await connection.close()
        [headers] = asked
        assert headers["User-Agent"] == "sattleme-Checkout/1"
        assert "Sec-WebSocket-Extensions" not in headers
    finally:
        server.close()
        await server.wait_closed()


async def test_a_websocket_redirect_is_never_followed() -> None:
    """A relay's URL is checked before it is asked (SPEC.md §9, §13): a redirect of the opening
    handshake would skip the check, as an HTTP redirect would (SPEC.md §15)."""
    reached: list[str] = []

    async def elsewhere(socket: websockets.asyncio.server.ServerConnection) -> None:
        reached.append("elsewhere")
        await socket.close()

    target = await websockets.asyncio.server.serve(elsewhere, "127.0.0.1", 0)
    target_port = target.sockets[0].getsockname()[1]

    def redirect(connection: Any, _request: Any) -> Any:
        response = connection.respond(http.HTTPStatus.FOUND, "")
        response.headers["Location"] = f"ws://127.0.0.1:{target_port}/"
        return response

    async def never(_socket: websockets.asyncio.server.ServerConnection) -> None:
        raise AssertionError("not a WebSocket")

    moved = await websockets.asyncio.server.serve(never, "127.0.0.1", 0, process_request=redirect)
    try:
        port = moved.sockets[0].getsockname()[1]
        with pytest.raises(websockets.InvalidStatus):
            await default_connect(f"ws://127.0.0.1:{port}")
        assert reached == []
    finally:
        for server in (moved, target):
            server.close()
            await server.wait_closed()


def _still_running() -> list[str]:
    """The tasks of `websockets` or of the package still running, but the current one."""
    found = []
    for task in asyncio.all_tasks():
        code = getattr(task.get_coro(), "cr_code", None)
        name = "" if code is None else code.co_filename
        if task is not asyncio.current_task() and ("websockets" in name or "sattleme" in name):
            found.append(task.get_coro().__qualname__)  # type: ignore[union-attr]
    return found


def _silent(closed: asyncio.Event) -> Callable[..., Any]:
    """A relay that accepts the opening handshake, then never answers anything, the closing
    handshake included; `closed` is set once the client closed the TCP connection."""

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        key = next(
            line.split(b":", 1)[1].strip()
            for line in request.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key:")
        )
        accept = base64.b64encode(
            hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()  # noqa: S324
        )
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        try:
            while await reader.read(65536):
                pass
        finally:
            closed.set()
            writer.close()

    return silent


async def test_a_connection_whose_closing_handshake_is_never_answered_ends_with_its_call() -> None:
    """SPEC.md §13: nothing a call started runs on once it returned. A relay that never answers
    the closing handshake: once the call's pool closed, within the close's bound, its TCP
    connection is closed and no task of `websockets` or of the package runs on (`websockets`'
    keepalive task kept it open until its next ping, some 20 s later)."""
    closed = asyncio.Event()
    server = await asyncio.start_server(_silent(closed), "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]

        async def connect(_url: str) -> Any:
            return await default_connect(f"ws://127.0.0.1:{port}")

        pool = RelayPool(connect=connect, timeout_ms=200, random=secrets.token_bytes, now=lambda: 0)
        async with pool:
            with pytest.raises(NetworkError):
                await pool.read(["wss://a.example"], {"kinds": [1]}, lambda value: value)
        started = time.monotonic()
        await asyncio.wait_for(closed.wait(), 0.5)
        assert time.monotonic() - started < 0.5
        assert _still_running() == []
    finally:
        server.close()
        await server.wait_closed()


async def test_a_call_cancelled_while_it_closes_returns_once_closed() -> None:
    """SPEC.md §13: nothing a call started runs on once it returned, a cancelled one's included.
    A call cancelled while its pool closes the connection of a relay that never answers the
    closing handshake (a `Client` call's timeout that runs out then, a second cancellation)
    returns once that connection is closed, never before: its close went on after the call had
    returned, its task and the connection with it."""
    closed = asyncio.Event()
    server = await asyncio.start_server(_silent(closed), "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]

        async def connect(_url: str) -> Any:
            return await default_connect(f"ws://127.0.0.1:{port}")

        async def call() -> None:
            pool = RelayPool(
                connect=connect, timeout_ms=200, random=secrets.token_bytes, now=lambda: 0
            )
            async with pool:
                with pytest.raises(NetworkError):
                    await pool.read(["wss://a.example"], {"kinds": [1]}, lambda value: value)

        task = asyncio.ensure_future(call())
        # The read ran out of time (0.2 s); its pool closes, which takes a second here.
        await asyncio.sleep(0.5)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _still_running() == []
        await asyncio.wait_for(closed.wait(), 0.5)
    finally:
        server.close()
        await server.wait_closed()
