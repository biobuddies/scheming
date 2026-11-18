"""Store verified upstream responses and write their original bytes."""

import sqlite3
from hashlib import sha256
from http.client import HTTPMessage
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from time import monotonic, sleep
from typing import IO, override
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile

type CachedResponse = tuple[bytes, str, str | None, str | None]


def is_source_url(url: str, hosts: set[str]) -> bool:
    parts = urlsplit(url)
    return parts.scheme == 'https' and parts.netloc in hosts and parts.username is None


def source_basename(url: str) -> str:
    return unquote(PurePosixPath(urlsplit(url).path).name)


class ScopedRedirects(HTTPRedirectHandler):
    """Follow HTTPS redirects within the source hosts."""

    def __init__(self, hosts: set[str]) -> None:
        self.hosts = hosts

    @override
    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str
    ) -> Request | None:
        if not is_source_url(newurl, self.hosts):
            raise ValueError(f'Unexpected redirect: {newurl}')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Cache:
    """Store each complete response and its validators in one SQLite transaction."""

    def __init__(self, connection: sqlite3.Connection, hosts: set[str], *, offline: bool) -> None:
        self.connection = connection
        self.hosts = hosts
        self.offline = offline
        self.opener = build_opener(ScopedRedirects(hosts))
        self.next_request_seconds = 0.0
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS responses (
                url TEXT PRIMARY KEY, body BLOB, hash TEXT, etag TEXT, modified TEXT
            )
        """)

    def get(
        self,
        url: str,
        *,
        filename: str = '',
        refresh: bool = False,
        release: str = '',
        seed: bytes | None = None,
    ) -> bytes:
        """Reuse an intact response or issue a single GET for a miss or refresh."""
        if not is_source_url(url, self.hosts):
            raise ValueError(f'Unexpected source URL: {url}')
        key = f'{release} {url}' if release else url
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            cached: CachedResponse | None = self.connection.execute(
                'SELECT body, hash, etag, modified FROM responses WHERE url = ?', (key,)
            ).fetchone()
            if cached is None and seed is not None:
                cached = (seed, sha256(seed).hexdigest(), None, None)
                self._store(key, cached)
            if cached is not None:
                body, digest, _, _ = cached
                if sha256(body).hexdigest() != digest:
                    raise ValueError(f'Cache integrity check failed for {url}')
                if not refresh:
                    return body
            if self.offline:
                raise ValueError(f'Offline cache miss for {url}')
            response = self._download(url, filename or source_basename(url), cached)
            self._store(key, response)
            return response[0]

    def _download(self, url: str, filename: str, cached: CachedResponse | None) -> CachedResponse:
        headers = {'User-Agent': 'biobuddies/scheming (documentation mirror)'}
        if cached is not None:
            _, _, etag, modified = cached
            if etag:
                headers['If-None-Match'] = etag
            if modified:
                headers['If-Modified-Since'] = modified

        sleep(max(0, self.next_request_seconds - monotonic()))
        print(f'GET {url}', flush=True)  # noqa: T201
        try:
            request = Request(url, headers=headers)  # noqa: S310 is_source_url checked by get
            with self.opener.open(request, timeout=40) as response:
                if response.status != 200:
                    raise ValueError(f'Unexpected HTTP status {response.status} for {url}')
                body = response.read()
                validate(filename, body)
                return (
                    body,
                    sha256(body).hexdigest(),
                    response.headers.get('ETag'),
                    response.headers.get('Last-Modified'),
                )
        except HTTPError as error:
            error.close()
            if error.code != 304 or cached is None:
                raise
            return cached
        finally:
            self.next_request_seconds = monotonic() + 2

    def _store(self, key: str, response: CachedResponse) -> None:
        self.connection.execute(
            'INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?, ?)', (key, *response)
        )


def replace(path: Path, body: bytes) -> None:
    if path.exists() and path.read_bytes() == body:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete_on_close=False) as temporary:
        temporary.write(body)
        temporary.close()
        Path(temporary.name).replace(path)


def validate(filename: str, body: bytes) -> None:
    if filename.endswith('.pdf') and not body.startswith(b'%PDF-'):
        raise ValueError(f'Invalid PDF: {filename}')
    if filename.endswith('.zip'):
        with ZipFile(BytesIO(body)) as archive:
            if not archive.namelist() or archive.testzip() is not None:
                raise ValueError(f'Invalid ZIP: {filename}')
    if filename.endswith('.html') and b'<html' not in body.lower():
        raise ValueError(f'Invalid HTML: {filename}')
