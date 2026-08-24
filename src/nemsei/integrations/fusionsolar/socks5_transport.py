"""A `FusionSolarTransport` implementation that routes over a local SOCKS5
proxy (e.g. `ssh -R 1080` reverse dynamic forwarding from a network whose
outbound IP is not blocked by FusionSolar -- see
docs/v2/FUSIONSOLAR_OWNERSHIP_WINDOW.md's "confirmed: the block is scoped
to the server's outbound IP" section).

Diagnostic/bridging tool, not the default production transport
(`UrllibFusionSolarTransport` in client.py remains that) -- this exists so
the real service classes (with their real session cache, real ownership
broker enforcement, real budget guard) can be pointed at an unblocked
network path for as long as an operator has one open, without becoming a
permanent dependency baked into the default client.

Stdlib only: speaks the unauthenticated SOCKS5 CONNECT handshake over a raw
socket, wraps it in TLS, and implements just enough of HTTP/1.1 (chunked or
Content-Length bodies, cookie persistence across calls on the same
instance) to satisfy FusionSolarClient's needs. Not a general-purpose HTTP
client -- POST with a JSON body and a small, known set of response shapes
is the only thing it needs to do correctly.
"""
from __future__ import annotations

import json
import socket
import ssl
from typing import Any
from urllib.parse import urlsplit

from nemsei.integrations.fusionsolar.client import FusionSolarClientError, HttpResponse
from nemsei.providers.errors import ProviderError, ProviderErrorCode


class Socks5ConnectError(ConnectionError):
    pass


def _socks5_connect(proxy_host: str, proxy_port: int, dest_host: str, dest_port: int, timeout: float) -> socket.socket:
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    sock.sendall(b"\x05\x01\x00")
    reply = sock.recv(2)
    if reply != b"\x05\x00":
        sock.close()
        raise Socks5ConnectError(f"SOCKS5 proxy at {proxy_host}:{proxy_port} did not accept no-auth: {reply!r}")
    dest_bytes = dest_host.encode("ascii")
    request = b"\x05\x01\x00\x03" + bytes([len(dest_bytes)]) + dest_bytes + dest_port.to_bytes(2, "big")
    sock.sendall(request)
    reply = sock.recv(4)
    if len(reply) < 4 or reply[1] != 0x00:
        sock.close()
        raise Socks5ConnectError(f"SOCKS5 CONNECT to {dest_host}:{dest_port} failed: {reply!r}")
    atyp = reply[3]
    if atyp == 0x01:
        sock.recv(4 + 2)
    elif atyp == 0x03:
        length = sock.recv(1)[0]
        sock.recv(length + 2)
    elif atyp == 0x04:
        sock.recv(16 + 2)
    return sock


def _parse_http_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar (via proxy) sent a malformed HTTP response."))
    header_lines = raw[:header_end].decode("iso-8859-1").split("\r\n")
    status_code = int(header_lines[0].split(" ", 2)[1])
    headers: dict[str, str] = {}
    for line in header_lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip()] = value.strip()
    body = raw[header_end + 4:]
    if headers.get("Transfer-Encoding", "").lower() == "chunked":
        decoded = bytearray()
        rest = body
        while rest:
            line_end = rest.find(b"\r\n")
            if line_end == -1:
                break
            try:
                size = int(rest[:line_end], 16)
            except ValueError:
                break
            if size == 0:
                break
            decoded += rest[line_end + 2: line_end + 2 + size]
            rest = rest[line_end + 2 + size + 2:]
        body = bytes(decoded)
    return status_code, headers, body


class Socks5FusionSolarTransport:
    """One instance per session (like `UrllibFusionSolarTransport`): persists
    cookies across `.post()` calls so a login's session survives into
    subsequent data calls, exactly as the default transport does."""

    def __init__(self, *, proxy_host: str = "127.0.0.1", proxy_port: int = 1080, timeout: float = 30.0) -> None:
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._timeout = timeout
        self._cookies: dict[str, str] = {}
        self._last_xsrf: str | None = None

    def xsrf_token(self) -> str | None:
        return self._last_xsrf

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    def _store_cookies(self, headers: dict[str, str]) -> None:
        # http.client folds repeated headers; a raw parse here only sees the
        # last Set-Cookie line if the server sent several. Good enough for
        # FusionSolar's login response (one XSRF-TOKEN cookie observed in
        # practice); not a general-purpose cookie jar.
        for key, value in headers.items():
            if key.lower() == "set-cookie":
                pair = value.split(";", 1)[0]
                if "=" in pair:
                    name, _, val = pair.partition("=")
                    self._cookies[name.strip()] = val.strip()
                    if name.strip().lower() == "xsrf-token":
                        self._last_xsrf = val.strip()

    def post(self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout_seconds: int) -> HttpResponse:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if parts.scheme != "https":
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.CONFIGURATION, "Socks5FusionSolarTransport only supports https URLs."))

        raw_sock = _socks5_connect(self._proxy_host, self._proxy_port, host, port, self._timeout)
        context = ssl.create_default_context()
        tls_sock = context.wrap_socket(raw_sock, server_hostname=host)
        try:
            body = json.dumps(payload).encode("utf-8")
            all_headers = dict(headers)
            all_headers["Host"] = host
            all_headers["Content-Length"] = str(len(body))
            all_headers["Connection"] = "close"
            if self._cookies:
                all_headers["Cookie"] = self._cookie_header()
            header_text = "".join(f"{k}: {v}\r\n" for k, v in all_headers.items())
            request = f"POST {parts.path or '/'} HTTP/1.1\r\n{header_text}\r\n".encode("ascii") + body
            tls_sock.sendall(request)

            chunks = []
            tls_sock.settimeout(timeout_seconds)
            while True:
                try:
                    chunk = tls_sock.recv(65536)
                except socket.timeout as exc:
                    raise FusionSolarClientError(ProviderError(ProviderErrorCode.TIMEOUT, "FusionSolar (via proxy) request timed out.", transient=True)) from exc
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            tls_sock.close()

        status_code, resp_headers, resp_body = _parse_http_response(b"".join(chunks))
        self._store_cookies(resp_headers)
        try:
            resp_payload = json.loads(resp_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar (via proxy) returned invalid JSON.")) from exc
        if not isinstance(resp_payload, dict):
            raise FusionSolarClientError(ProviderError(ProviderErrorCode.INVALID_RESPONSE, "FusionSolar (via proxy) returned an unexpected response."))
        return HttpResponse(status_code, resp_headers, resp_payload)
