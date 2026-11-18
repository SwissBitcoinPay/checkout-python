"""HTTP (SPEC.md §15): GET only, no redirect followed, a timeout for the whole request, 1 MiB.

The transport is injectable (tests, proxies): any `HttpTransport`. Whatever it is, the package
bounds the whole request by its own timeout (it cancels the transport), refuses a body larger than
1 MiB and reads a redirect as no answer. The default transport is the standard library's
`http.client`, which never follows a redirect, run in a worker thread, its answer's head bounded
at 10 MiB in all (`http.client` bounds each line, not their count). It asks https URLs, and http
ones only of a merchant's Bitcoin node on a local network (SPEC.md §31: its URL's own rule).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import http.client
import io
import socket
import ssl
import sys
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from . import _json
from ._constants import HTTP_BODY_MAX_BYTES, HTTP_HEAD_MAX_BYTES
from ._crypto import Secret
from ._node import parse_node_url
from .errors import NetworkError

# The same in every implementation and version (SPEC.md §15): CoinGecko refuses a request without
# one, and one naming a language or a version would only help a server single it out.
USER_AGENT = "sattleme-Checkout/1"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """An HTTP answer: its status and its body (the transport reads at most `max_bytes` + 1)."""

    status: int
    body: bytes


class HttpTransport(Protocol):
    """How the package makes an HTTP GET. It must never follow a redirect.

    `socket_timeout` (seconds) is the whole request's: the package cancels the call when it is
    over; a transport that blocks (a thread) bounds its socket operations by it.
    `max_bytes`: a body longer than that is refused; reading more than `max_bytes` + 1 is useless.
    """

    async def __call__(
        self, url: str, *, headers: Mapping[str, str], socket_timeout: float, max_bytes: int
    ) -> HttpResponse: ...


class HeadTooLargeError(http.client.HTTPException):
    """An answer whose head is larger than 10 MiB: no answer (SPEC.md §15)."""


class _HeadBound(io.BufferedReader):
    """The socket's file of an answer, as `http.client` reads it, its head bounded (SPEC.md §15):
    what it reads by lines, which is all of the head (the status line and the headers, of an
    interim answer too, a chunked body's size lines, its trailers), counts toward 10 MiB in all.
    `http.client` bounds each line (64 KiB) and the count of headers, not that of the interim
    answers, of a chunked body's size lines nor, in some versions, of its trailers. The body is
    read otherwise (`read`, `readinto`), never by lines."""

    def __init__(self, raw: io.RawIOBase) -> None:
        super().__init__(raw)
        self._head = 0

    def readline(self, size: int | None = -1, /) -> bytes:
        line = super().readline(size)
        self._head += len(line)
        if self._head > HTTP_HEAD_MAX_BYTES:
            raise HeadTooLargeError("an answer's head larger than 10 MiB")
        return line


class _Response(http.client.HTTPResponse):
    """`http.client`'s answer, its head bounded (`_HeadBound`): the socket's file it made, nothing
    read from it yet, is given over to one that counts."""

    def __init__(self, sock: socket.socket, *args: Any, **kwargs: Any) -> None:
        super().__init__(sock, *args, **kwargs)
        self.fp = _HeadBound(self.fp.detach())


class _Connection(http.client.HTTPSConnection):
    """`http.client`'s HTTPS connection, which `abandon` (from the event loop's thread) ends
    wherever its worker thread is: the socket being connected, in its TLS handshake or read is
    shut at once, and no socket is made once it is abandoned. Its thread then ends at the latest
    when the name resolution under way returns (the system's resolver bounds it). Its answers'
    heads are bounded (`_Response`). `tls` False: in clear HTTP (a merchant's Bitcoin node on a
    local network, SPEC.md §31), the same otherwise."""

    response_class = _Response

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        context: ssl.SSLContext,
        tls: bool = True,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._tls = context if tls else None
        self._seconds = timeout
        self._abandoned = False
        # The socket in use. Not `self.sock`: `getresponse` clears it (`close`) when the answer
        # ends the connection, whose body is then still read through that socket.
        self._live: socket.socket | None = None

    def abandon(self) -> None:
        self._abandoned = True
        live = self._live
        if live is not None:
            with contextlib.suppress(OSError):
                # The socket's own shutdown: a TLS socket's would also drop its TLS state, which
                # the worker thread may be using. A socket closed already (its descriptor -1)
                # refuses it.
                socket.socket.shutdown(live, socket.SHUT_RDWR)

    def _go_on(self) -> None:
        if self._abandoned:
            raise ConnectionAbortedError("the request was cancelled")

    def connect(self) -> None:
        """As `http.client` does (`socket.create_connection`, TCP_NODELAY, then TLS), each
        socket in `self._live` from its creation on, so that `abandon` reaches it, and none made
        or used once abandoned."""
        sys.audit("http.client.connect", self, self.host, self.port)
        self._go_on()
        error: OSError = OSError(f"{self.host} has no address")
        for family, kind, proto, _name, address in socket.getaddrinfo(
            self.host, self.port, type=socket.SOCK_STREAM
        ):
            self._go_on()
            try:
                # A family the system cannot make a socket of: the next address.
                sock = socket.socket(family, kind, proto)
            except OSError as failure:
                error = failure
                continue
            self.sock = self._live = sock
            try:
                # Abandoned before `self._live` was set: no shutdown reached this socket.
                self._go_on()
                sock.settimeout(self._seconds)
                sock.connect(address)
                break
            except OSError as failure:
                sock.close()
                self.sock = self._live = None
                error = failure
        else:
            raise error
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as failure:
            # As `http.client`: a system may have no TCP_NODELAY.
            if failure.errno != errno.ENOPROTOOPT:
                raise
        if self._tls is None:
            return
        # The handshake once the TLS socket is `self._live`: `wrap_socket` takes the plain
        # socket over (it is detached, its descriptor -1), so a shutdown reaches only this one.
        tls = self._tls.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
        self.sock = self._live = tls
        try:
            self._go_on()
            tls.do_handshake()
            self._go_on()
        except BaseException:
            # As `wrap_socket` does when its handshake fails.
            self.sock = self._live = None
            tls.close()
            raise


def _scheme_allowed(parts: urllib.parse.SplitResult) -> bool:
    """https, or http to a merchant's Bitcoin node on a local network: its scheme and authority
    a node URL's (SPEC.md §31), which allows http for a local host alone."""
    if parts.scheme == "https":
        return True
    node = parse_node_url(f"{parts.scheme}://{parts.netloc}")
    return parts.scheme == "http" and node is not None and node.local


class DefaultTransport:
    """`http.client` over TLS, in a worker thread; no proxy. `context`: the TLS context (the
    system's trust store by default; a corporate CA's, for one). In clear HTTP only to a merchant's
    Bitcoin node on a local network (SPEC.md §31)."""

    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        self._context = ssl.create_default_context() if context is None else context

    async def __call__(
        self, url: str, *, headers: Mapping[str, str], socket_timeout: float, max_bytes: int
    ) -> HttpResponse:
        parts = urllib.parse.urlsplit(url)
        if not _scheme_allowed(parts) or not parts.hostname:
            raise ValueError(
                "the default transport only asks https URLs, and http ones of a merchant's "
                "Bitcoin node on a local network"
            )
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        tls = parts.scheme == "https"
        connection = _Connection(
            parts.hostname,
            parts.port or (443 if tls else 80),
            timeout=socket_timeout,
            context=self._context,
            tls=tls,
        )

        def get() -> HttpResponse:
            response: http.client.HTTPResponse | None = None
            try:
                # Connected first: a connection that fails (the common failure) leaves no frame
                # holding the request, whose headers may carry a key (CoinGecko's).
                connection.connect()
                connection.request("GET", target, headers=headers)
                response = connection.getresponse()
                return HttpResponse(response.status, response.read(max_bytes + 1))
            finally:
                # The answer holds the socket once the connection gave it over (an answer that
                # ends the connection): closed now, never when the answer is collected, which
                # the traceback of a failure (it holds this frame) puts off for as long as its
                # error is held.
                if response is not None:
                    response.close()
                connection.close()

        try:
            return await asyncio.to_thread(get)
        except asyncio.CancelledError:
            # The thread may be blocked on the socket, or about to make it: end it now.
            connection.abandon()
            raise


def without_bom(body: bytes) -> bytes:
    """A body without its one leading byte order mark, as WHATWG's UTF-8 decode (and so `fetch`)
    reads it (SPEC.md §15)."""
    return body[3:] if body.startswith(b"\xef\xbb\xbf") else body


NO_JSON = object()
"""The body of an answer that is not JSON in UTF-8."""


class _Headers(dict[str, str]):
    """The headers of one request, made at the transport's call (`_headers`). Its repr names
    them, never their values: one may carry a key (CoinGecko's), and a frame that holds them, a
    transport's, shows no key where an error tracker captures its local variables."""

    def __repr__(self) -> str:
        return "{" + ", ".join(f"{name!r}: ..." for name in self) + "}"


def _headers(headers: Mapping[str, str] | None, secrets: Mapping[str, Secret] | None) -> _Headers:
    """A request's headers: `headers`, then `secrets` (ASCII), revealed here and nowhere before,
    then the package's own, which no caller replaces."""
    made = _Headers(headers or {})
    for name, secret in (secrets or {}).items():
        made[name] = secret.reveal().decode("ascii")
    made["Accept"] = "application/json"
    made["User-Agent"] = USER_AGENT
    return made


@dataclass(frozen=True, slots=True)
class Http:
    """The package's HTTP: a transport and the timeout of each request."""

    transport: HttpTransport
    timeout_ms: int

    def _host(self, url: str) -> str:
        # Messages name the host only: a Lightning address's name is in its pay URL's path.
        return urllib.parse.urlsplit(url).hostname or "(invalid URL)"

    async def get(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        secret_headers: Mapping[str, Secret] | None = None,
    ) -> HttpResponse:
        """GET `url`; NetworkError for no answer, a redirect, or a body larger than 1 MiB.

        `secret_headers`: headers whose values are secrets (a key), revealed only in the headers
        made at the transport's call: no frame of the package's holds them before."""
        timeout = self.timeout_ms / 1000
        try:
            async with asyncio.timeout(timeout):
                response = await self.transport(
                    url,
                    headers=_headers(headers, secret_headers),
                    socket_timeout=timeout,
                    max_bytes=HTTP_BODY_MAX_BYTES,
                )
        except TimeoutError as error:
            raise NetworkError(f"{self._host(url)} did not answer in time") from error
        except NetworkError:
            raise
        except Exception as error:
            raise NetworkError(f"{self._host(url)} did not answer") from error
        # The transport may be anyone's: what it gives is checked as any other input.
        answer: object = response
        status: object = getattr(answer, "status", None)
        body: object = getattr(answer, "body", None)
        if (
            not isinstance(answer, HttpResponse)
            or isinstance(status, bool)
            or not isinstance(status, int)
            or not isinstance(body, bytes)
        ):
            raise NetworkError(f"{self._host(url)} gave no answer the package reads")
        if 300 <= response.status < 400:
            raise NetworkError(f"{self._host(url)} answered with a redirect, never followed")
        if len(response.body) > HTTP_BODY_MAX_BYTES:
            raise NetworkError(f"{self._host(url)} answered with a body larger than 1 MiB")
        return response

    async def get_json(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        secret_headers: Mapping[str, Secret] | None = None,
    ) -> tuple[int, object]:
        """GET `url`: its status and its JSON body (NO_JSON when it is not JSON in UTF-8)."""
        response = await self.get(url, headers, secret_headers=secret_headers)
        value = _json.parse_bytes(without_bom(response.body))
        return response.status, NO_JSON if value is None else value
