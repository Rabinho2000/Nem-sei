#!/usr/bin/env python3
"""One-shot FusionSolar login probe, routed through a local SOCKS5 proxy
(e.g. an `ssh -R 1080 ...` reverse dynamic forward). Stdlib only -- no
PySocks/requests -- speaks the unauthenticated SOCKS5 CONNECT handshake
directly over a raw socket, then does the TLS handshake and HTTP request
on top of it.

Run at most once per sitting -- same discipline as fusionsolar_login_probe.py.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import ssl
import sys


def socks5_connect(proxy_host: str, proxy_port: int, dest_host: str, dest_port: int, timeout: float = 15.0) -> socket.socket:
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    sock.sendall(b"\x05\x01\x00")  # version 5, 1 auth method, no-auth
    reply = sock.recv(2)
    if reply != b"\x05\x00":
        raise ConnectionError(f"SOCKS5 proxy did not accept no-auth: {reply!r}")
    dest_bytes = dest_host.encode("ascii")
    request = b"\x05\x01\x00\x03" + bytes([len(dest_bytes)]) + dest_bytes + dest_port.to_bytes(2, "big")
    sock.sendall(request)
    reply = sock.recv(4)
    if len(reply) < 4 or reply[1] != 0x00:
        raise ConnectionError(f"SOCKS5 CONNECT failed, reply={reply!r}")
    atyp = reply[3]
    if atyp == 0x01:
        sock.recv(4 + 2)
    elif atyp == 0x03:
        length = sock.recv(1)[0]
        sock.recv(length + 2)
    elif atyp == 0x04:
        sock.recv(16 + 2)
    return sock


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--host", default="eu5.fusionsolar.huawei.com")
    parser.add_argument("--proxy-host", default="127.0.0.1")
    parser.add_argument("--proxy-port", type=int, default=1080)
    args = parser.parse_args()

    password = os.environ.get("FUSIONSOLAR_PROBE_PASSWORD") or getpass.getpass("FusionSolar password (not echoed): ")
    if not password:
        print("No password provided.", file=sys.stderr)
        return 2

    print(f"Connecting to SOCKS5 proxy at {args.proxy_host}:{args.proxy_port} ...")
    raw_sock = socks5_connect(args.proxy_host, args.proxy_port, args.host, 443)

    context = ssl.create_default_context()
    tls_sock = context.wrap_socket(raw_sock, server_hostname=args.host)

    payload = json.dumps({"userName": args.username, "systemCode": password}).encode("utf-8")
    request = (
        f"POST /thirdData/login HTTP/1.1\r\n"
        f"Host: {args.host}\r\n"
        f"Content-Type: application/json\r\n"
        f"Accept: application/json, */*\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("ascii") + payload

    print(f"Sending ONE login request for {args.username!r} via the proxy ...")
    tls_sock.sendall(request)

    chunks = []
    while True:
        chunk = tls_sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    tls_sock.close()

    response = b"".join(chunks)
    header_end = response.find(b"\r\n\r\n")
    headers_raw = response[:header_end].decode("iso-8859-1")
    body = response[header_end + 4:]

    status_line = headers_raw.splitlines()[0]
    print(f"\n{status_line}")
    has_xsrf = "xsrf-token" in headers_raw.lower()
    print(f"XSRF-TOKEN header present: {has_xsrf}")

    # Body may be chunked-encoded; strip chunk framing crudely if present.
    if "transfer-encoding: chunked" in headers_raw.lower():
        decoded = bytearray()
        rest = body
        while rest:
            line_end = rest.find(b"\r\n")
            if line_end == -1:
                break
            size_line = rest[:line_end]
            try:
                size = int(size_line, 16)
            except ValueError:
                break
            if size == 0:
                break
            decoded += rest[line_end + 2: line_end + 2 + size]
            rest = rest[line_end + 2 + size + 2:]
        body = bytes(decoded)

    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None

    if isinstance(parsed, dict):
        print(f"success: {parsed.get('success')}")
        print(f"failCode: {parsed.get('failCode')}")
        print(f"message: {parsed.get('message')}")
        if parsed.get("success") is True and parsed.get("failCode") in (0, None):
            print("\n=> LOGIN SUCCEEDED through the proxy. This network path is not blocked.")
            return 0
        print("\n=> Login was rejected -- see failCode/message above.")
        return 1

    print("\nCould not parse response body as JSON:")
    print(body[:500])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
