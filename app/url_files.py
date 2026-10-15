"""Bounded URL imports with per-hop validation and connections pinned to validated IPs."""

import asyncio
import http.client
import ipaddress
import mimetypes
import socket
import ssl
import tempfile
import time
from dataclasses import dataclass
from email.message import Message
from pathlib import PurePosixPath
from typing import BinaryIO
from urllib.parse import unquote, urljoin, urlsplit

from fastapi import HTTPException


@dataclass
class RemoteFile:
    file: BinaryIO
    filename: str
    content_type: str
    size: int


def _resolve(url: str, allowed_hosts: list[str]):
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if (
            parts.scheme not in {"http", "https"}
            or not host
            or parts.port == 0
            or "%" in host
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)
            or "\\" in url
        ):
            raise ValueError
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = list(dict.fromkeys(item[4][0] for item in addresses))
        if not ips:
            raise OSError
        allowed = host in {value.strip().lower().rstrip(".") for value in allowed_hosts}
        if not allowed and any(
            not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast
            for ip in ips
        ):
            raise HTTPException(
                422, "URL host is not public; configure an explicit import allowed host"
            )
        return parts, host, port, ips[0]
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(
            422, "file_url must be an HTTP(S) URL without credentials or fragment"
        ) from exc
    except OSError as exc:
        raise HTTPException(502, "URL host resolution failed") from exc


def _open(url: str, allowed_hosts: list[str], timeout: float):
    parts, host, port, ip = _resolve(url, allowed_hosts)
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    # HTTPSConnection keeps the original host for SNI/certificate checks; only TCP is pinned.
    connection._create_connection = lambda _address, timeout, source_address: (
        socket.create_connection(
            (ip, port),
            timeout=timeout,
            source_address=source_address,
        )
    )
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        connection.request("GET", path, headers={"Accept-Encoding": "identity"})
        return connection, connection.getresponse()
    except BaseException:
        connection.close()
        raise


def download_file(
    file_url: str,
    filename: str | None,
    *,
    max_bytes: int,
    timeout_seconds: float,
    allowed_hosts: list[str],
    max_redirects: int = 3,
) -> RemoteFile:
    stream = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    deadline = time.monotonic() + timeout_seconds
    try:
        for hop in range(max_redirects + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection, response = _open(file_url, allowed_hosts, remaining)
            try:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location or hop == max_redirects:
                        raise HTTPException(422, "URL redirect limit exceeded or missing Location")
                    file_url = urljoin(file_url, location)
                    continue
                if response.status != 200:
                    raise HTTPException(502, f"URL download returned HTTP {response.status}")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise HTTPException(422, "URL download must use identity Content-Encoding")
                length = response.getheader("Content-Length")
                if length is not None:
                    try:
                        length = int(length)
                    except ValueError as exc:
                        raise HTTPException(502, "Invalid URL response Content-Length") from exc
                    if length < 0:
                        raise HTTPException(502, "Invalid URL response Content-Length")
                    if length > max_bytes:
                        raise HTTPException(413, "URL file exceeds import size limit")
                total = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    # read1 closes fp as soon as a known-length or chunked body is exhausted.
                    if response.fp is None:
                        break
                    # HTTP/1.0 may detach the socket from connection after response headers.
                    response.fp.raw._sock.settimeout(remaining)
                    chunk = response.read1(min(65536, max_bytes - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(413, "URL file exceeds import size limit")
                    stream.write(chunk)
                if length is not None and total != length:
                    raise HTTPException(502, "URL download was incomplete")
                disposition = Message()
                disposition["content-disposition"] = response.getheader("Content-Disposition", "")
                name = (
                    filename
                    or disposition.get_filename()
                    or unquote(
                        PurePosixPath(urlsplit(file_url).path).name,
                    )
                    or "download"
                )
                name = str(name).replace("\\", "/").split("/")[-1]
                name = (
                    "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:180] or "download"
                )
                content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip()
                if not content_type or content_type == "application/octet-stream":
                    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                stream.seek(0)
                return RemoteFile(stream, name, content_type, total)
            finally:
                response.close()
                connection.close()
        raise HTTPException(422, "URL redirect limit exceeded")
    except BaseException as exc:
        stream.close()
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, TimeoutError):
            raise HTTPException(504, "URL download timed out") from exc
        if isinstance(exc, (OSError, http.client.HTTPException, UnicodeError, ValueError)):
            # Never echo source URLs: signed query parameters contain credentials.
            raise HTTPException(502, "URL download failed") from exc
        raise


async def fetch_file(*args, **kwargs) -> RemoteFile:
    task = asyncio.create_task(asyncio.to_thread(download_file, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:

        def close_result(done):
            if not done.cancelled() and done.exception() is None:
                done.result().file.close()

        task.add_done_callback(close_result)
        raise
