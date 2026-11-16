#!/usr/bin/env python3
"""HTTP/HTTPS (CONNECT) and SOCKS5 forward proxy. No TLS interception."""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import socket
import struct
from typing import Iterable

BUFFER = 65536
log = logging.getLogger("proxy")

HOP_BY_HOP = {
    "proxy-authorization",
    "proxy-connection",
    "connection",
    "keep-alive",
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _peer_ip(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    return peer[0] if peer else "?"


def _auth_ok(headers: dict[str, str], user: str, password: str) -> bool:
    if not user:
        return True
    raw = headers.get("proxy-authorization", "")
    if not raw.lower().startswith("basic "):
        return False
    try:
        got = base64.b64decode(raw.split(" ", 1)[1]).decode()
    except Exception:
        return False
    return got == f"{user}:{password}"


def _parse_http_head(data: bytes) -> tuple[str, str, str, dict[str, str], bytes]:
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    method, target, version = lines[0].split(" ", 2)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return method, target, version, headers, body


def _host_port(target: str, default_port: int) -> tuple[str, int]:
    if not target:
        raise ValueError("empty host")
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        if rest.startswith(":") and rest[1:]:
            return host, int(rest[1:])
        return host, default_port
    if target.count(":") == 1:
        host, port_s = target.rsplit(":", 1)
        if port_s.isdigit():
            return host, int(port_s)
    return target, default_port


def _allowed(ip: str, allow: set[str]) -> bool:
    if not allow:
        return True
    return ip in allow or ip in {"127.0.0.1", "::1"}


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(BUFFER)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError, OSError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


async def _tunnel(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    remote_r: asyncio.StreamReader,
    remote_w: asyncio.StreamWriter,
    leftover: bytes = b"",
) -> None:
    if leftover:
        remote_w.write(leftover)
        await remote_w.drain()
    t1 = asyncio.create_task(_pipe(client_r, remote_w))
    t2 = asyncio.create_task(_pipe(remote_r, client_w))
    try:
        await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        t1.cancel()
        t2.cancel()
        for w in (client_w, remote_w):
            try:
                w.close()
            except Exception:
                pass


async def _open(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(host, port)


async def handle_http(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    user: str,
    password: str,
    allow: set[str],
) -> None:
    ip = _peer_ip(writer)
    try:
        if not _allowed(ip, allow):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        try:
            data = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return

        method, target, version, headers, body = _parse_http_head(data)

        if not _auth_ok(headers, user, password):
            writer.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b'Proxy-Authenticate: Basic realm="proxy"\r\n'
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            return

        if method.upper() == "CONNECT":
            host, port = _host_port(target, 443)
            log.info("%s CONNECT %s:%s", ip, host, port)
            try:
                remote_r, remote_w = await _open(host, port)
            except OSError as e:
                log.warning("CONNECT failed %s:%s (%s)", host, port, e)
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await _tunnel(reader, writer, remote_r, remote_w, body)
            return

        if target.startswith("http://"):
            rest = target[len("http://") :]
            hostport, _, path = rest.partition("/")
            path = "/" + path
            host, port = _host_port(hostport, 80)
        else:
            host, port = _host_port(headers.get("host", ""), 80)
            path = target

        log.info("%s %s http://%s:%s%s", ip, method, host, port, path)
        lines = [f"{method} {path} {version}"]
        for k, v in headers.items():
            if k not in HOP_BY_HOP:
                lines.append(f"{k}: {v}")
        lines.append("Connection: close")
        lines.append("")
        lines.append("")
        try:
            remote_r, remote_w = await _open(host, port)
        except OSError as e:
            log.warning("HTTP failed %s:%s (%s)", host, port, e)
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        remote_w.write("\r\n".join(lines).encode("iso-8859-1") + body)
        await remote_w.drain()
        await _tunnel(reader, writer, remote_r, remote_w)
    except Exception as e:
        log.debug("http conn error: %s", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _socks_userpass(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, user: str, password: str
) -> bool:
    ver = await reader.readexactly(1)
    if ver != b"\x01":
        return False
    ulen = (await reader.readexactly(1))[0]
    uname = (await reader.readexactly(ulen)).decode("utf-8", "replace")
    plen = (await reader.readexactly(1))[0]
    passwd = (await reader.readexactly(plen)).decode("utf-8", "replace")
    ok = uname == user and passwd == password
    writer.write(b"\x01\x00" if ok else b"\x01\x01")
    await writer.drain()
    return ok


async def handle_socks(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    user: str,
    password: str,
    allow: set[str],
) -> None:
    ip = _peer_ip(writer)
    try:
        if not _allowed(ip, allow):
            writer.close()
            return

        ver, nmethods = await reader.readexactly(2)
        if ver != 5:
            return
        methods = set(await reader.readexactly(nmethods))
        if user:
            if 2 not in methods:
                writer.write(b"\x05\xff")
                await writer.drain()
                return
            writer.write(b"\x05\x02")
            await writer.drain()
            if not await _socks_userpass(reader, writer, user, password):
                return
        else:
            if 0 not in methods:
                writer.write(b"\x05\xff")
                await writer.drain()
                return
            writer.write(b"\x05\x00")
            await writer.drain()

        req = await reader.readexactly(4)
        if req[0] != 5 or req[1] != 1:  # CONNECT only
            writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            return
        atyp = req[3]
        if atyp == 1:
            host = socket.inet_ntoa(await reader.readexactly(4))
        elif atyp == 3:
            ln = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(ln)).decode("idna")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        else:
            writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            return
        port = struct.unpack("!H", await reader.readexactly(2))[0]

        log.info("%s SOCKS5 CONNECT %s:%s", ip, host, port)
        try:
            remote_r, remote_w = await _open(host, port)
        except OSError as e:
            log.warning("SOCKS failed %s:%s (%s)", host, port, e)
            writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            return

        bind = remote_w.get_extra_info("sockname") or ("0.0.0.0", 0)
        bind_ip, bind_port = bind[0], bind[1]
        try:
            addr = socket.inet_aton(bind_ip)
            reply = b"\x05\x00\x00\x01" + addr + struct.pack("!H", bind_port)
        except OSError:
            reply = b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        writer.write(reply)
        await writer.drain()
        await _tunnel(reader, writer, remote_r, remote_w)
    except (asyncio.IncompleteReadError, ConnectionResetError, OSError) as e:
        log.debug("socks conn error: %s", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="HTTP/HTTPS + SOCKS5 forward proxy")
    p.add_argument("--host", default=_env("PROXY_HOST", "127.0.0.1"))
    p.add_argument(
        "--http-port",
        type=int,
        default=int(_env("PROXY_HTTP_PORT") or _env("PORT") or "8888"),
    )
    p.add_argument("--socks-port", type=int, default=int(_env("PROXY_SOCKS_PORT", "1080")))
    p.add_argument("--user", default=_env("PROXY_USER", ""))
    p.add_argument("--password", default=_env("PROXY_PASSWORD", ""))
    p.add_argument(
        "--allow",
        default=_env("PROXY_ALLOW", ""),
        help="comma-separated client IPs; empty = any (still use auth on public binds)",
    )
    p.add_argument("--no-socks", action="store_true", help="disable SOCKS5 listener")
    return p.parse_args(list(argv) if argv is not None else None)


async def serve(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    allow = {x.strip() for x in args.allow.split(",") if x.strip()}
    public = args.host not in {"127.0.0.1", "::1", "localhost"}
    if public and (not args.user or not args.password):
        raise SystemExit("public bind requires --user and --password (or PROXY_USER / PROXY_PASSWORD)")

    http_srv = await asyncio.start_server(
        lambda r, w: handle_http(r, w, args.user, args.password, allow),
        args.host,
        args.http_port,
        limit=262144,
    )
    socks_srv = None
    if not args.no_socks:
        socks_srv = await asyncio.start_server(
            lambda r, w: handle_socks(r, w, args.user, args.password, allow),
            args.host,
            args.socks_port,
        )

    log.info("HTTP/HTTPS proxy on %s:%s", args.host, args.http_port)
    if socks_srv:
        log.info("SOCKS5 proxy on %s:%s", args.host, args.socks_port)
    log.info("auth=%s allow=%s", bool(args.user), ",".join(sorted(allow)) or "*")

    async with http_srv:
        if socks_srv:
            async with socks_srv:
                await asyncio.gather(http_srv.serve_forever(), socks_srv.serve_forever())
        else:
            await http_srv.serve_forever()


def run() -> None:
    try:
        asyncio.run(serve(parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
