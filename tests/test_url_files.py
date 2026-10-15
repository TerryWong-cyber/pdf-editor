"""Exercise URL policy and bounded downloads without external network access."""

import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import url_files


class Response:
    def __init__(self, body=b"hello", status=200, headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = headers or {}
        self.closed = False
        self.fp = SimpleNamespace(
            raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=lambda _: None))
        )

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, size):
        return self.body.read(size)

    def close(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    responses, requests = [], []
    addresses = {"public.example": "93.184.216.34", "private.example": "127.0.0.1"}
    monkeypatch.setattr(
        url_files.socket,
        "getaddrinfo",
        lambda host, port, **kw: [
            (2, 1, 6, "", (addresses.get(host, host), port)),
        ],
    )

    class Connection:
        def __init__(self, host, port, **kwargs):
            self.host = host
            self.closed = False

        def request(self, method, path, headers):
            requests.append((self.host, method, path, headers))

        def getresponse(self):
            return responses.pop(0)

        def close(self):
            self.closed = True

    monkeypatch.setattr(url_files.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(url_files.http.client, "HTTPSConnection", Connection)
    return responses, requests


def fetch(url="https://public.example/file.txt", **kwargs):
    return url_files.download_file(
        url,
        kwargs.pop("filename", None),
        max_bytes=kwargs.pop("max_bytes", 100),
        timeout_seconds=10,
        allowed_hosts=kwargs.pop("allowed_hosts", []),
        **kwargs,
    )


def test_download_preserves_query_and_filename(transport):
    responses, requests = transport
    response = Response(
        headers={
            "Content-Length": "5",
            "Content-Disposition": 'attachment; filename="../notes.txt"',
        }
    )
    responses.append(response)
    result = fetch("https://public.example/download?signature=private")
    try:
        assert result.file.read() == b"hello"
        assert result.filename == "notes.txt"
        assert result.content_type == "text/plain"
        assert result.size == 5
        assert requests[0][2] == "/download?signature=private"
        assert requests[0][3] == {"Accept-Encoding": "identity"}
        assert response.closed
    finally:
        result.file.close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://public.example/a",
        "https://user:secret@public.example/a",
        "https://public.example/a#fragment",
        "https://public.example:bad/a",
        "http://public.example/\r\nsecret",
        "http://public.example\\@private.example/a",
    ],
)
def test_invalid_urls_are_rejected(transport, url):
    with pytest.raises(HTTPException) as error:
        fetch(url)
    assert error.value.status_code == 422
    assert "secret" not in str(error.value.detail)


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "224.0.0.1"])
def test_non_public_addresses_are_rejected(transport, host):
    authority = f"[{host}]" if ":" in host else host
    with pytest.raises(HTTPException, match="not public"):
        fetch(f"http://{authority}/file")


def test_explicit_private_host_allowlist(transport):
    transport[0].append(Response())
    result = fetch("http://private.example/file", allowed_hosts=["private.example"])
    result.file.close()
    assert transport[1][0][0] == "private.example"


def test_redirect_is_checked_again(transport):
    transport[0].append(Response(status=302, headers={"Location": "http://private.example/secret"}))
    with pytest.raises(HTTPException, match="not public"):
        fetch()
    assert len(transport[1]) == 1


def test_relative_redirect_and_filename_override(transport):
    transport[0].extend([Response(status=302, headers={"Location": "/next"}), Response()])
    result = fetch(filename="override.txt")
    result.file.close()
    assert result.filename == "override.txt"
    assert transport[1][-1][2] == "/next"


@pytest.mark.parametrize(
    "body,headers",
    [
        (b"hello", {"Content-Length": "500"}),
        (b"x" * 101, {}),
    ],
)
def test_size_limit_checks_header_and_actual_stream(transport, body, headers):
    transport[0].append(Response(body=body, headers=headers))
    with pytest.raises(HTTPException) as error:
        fetch()
    assert error.value.status_code == 413


def test_incomplete_download_is_rejected(transport):
    transport[0].append(Response(headers={"Content-Length": "9"}))
    with pytest.raises(HTTPException, match="incomplete"):
        fetch()


def test_errors_do_not_expose_signed_url(transport):
    transport[0].append(Response(status=403))
    with pytest.raises(HTTPException) as error:
        fetch("https://public.example/a?token=private-secret")
    assert error.value.status_code == 502
    assert "private-secret" not in str(error.value.detail)


def test_socket_connects_to_the_validated_ip(transport, monkeypatch):
    transport[0].append(Response())
    calls = []
    monkeypatch.setattr(
        url_files.socket, "create_connection", lambda address, **kw: calls.append(address)
    )
    connection, response = url_files._open("https://public.example/a", [], 10)
    connection._create_connection(("public.example", 443), 10, None)
    assert calls == [("93.184.216.34", 443)]
    assert connection.host == "public.example"
    response.close()


def test_redirect_limit(transport):
    transport[0].extend(Response(status=302, headers={"Location": "/again"}) for _ in range(4))
    with pytest.raises(HTTPException, match="redirect limit"):
        fetch()


def test_mixed_public_and_private_dns_answer_is_rejected(monkeypatch):
    monkeypatch.setattr(
        url_files.socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(HTTPException, match="not public"):
        fetch()


def test_socket_timeout_becomes_gateway_timeout(transport, monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(url_files, "_open", timeout)
    with pytest.raises(HTTPException) as error:
        fetch()
    assert error.value.status_code == 504


def test_total_deadline_is_enforced(transport, monkeypatch):
    transport[0].append(Response())
    ticks = iter([0, 0, 11])
    monkeypatch.setattr(url_files.time, "monotonic", lambda: next(ticks))
    with pytest.raises(HTTPException) as error:
        fetch()
    assert error.value.status_code == 504
