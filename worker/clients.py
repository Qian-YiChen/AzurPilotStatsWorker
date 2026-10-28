import hashlib
import logging
import time
import threading
import re
from pathlib import Path
from urllib.parse import urlparse, urljoin

import httpx

log = logging.getLogger(__name__)

# 123 Pan drops connections under load. Retry the same original in place so a
# single timeout cannot fail a whole group and re-download every image.
TRANSIENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteError,
             httpx.RemoteProtocolError, httpx.ProxyError)


class Retryable(Exception):
    """Transient cloud response; the same original download is attempted again."""


def retrying(operation, attempts=3):
    delay = 0.25
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except TRANSIENT + (Retryable,) as exc:
            if attempt == attempts:
                raise
            # Signed download URLs must never reach the logs, so log the type.
            log.warning('download retry attempt=%s error=%s', attempt, type(exc).__name__)
            time.sleep(delay)
            delay *= 2


class Statistics:
    def __init__(self, config):
        self.client = httpx.Client(base_url=config.c_url, headers={"Authorization": "Bearer " + config.c_token}, timeout=60)

    def post(self, path, data):
        r = self.client.post(path, json=data)
        r.raise_for_status()
        return r.json()

    def revision(self, sha):
        r = self.client.get("/internal/v1/statistics/results/" + sha)
        if r.status_code == 404:
            return 0
        r.raise_for_status()
        return int(r.json()["revision"])

    def release(self, id_):
        r = self.client.get(f"/internal/v1/template-releases/{id_}")
        r.raise_for_status()
        if len(r.content) > 32 * 1024**2 or hashlib.sha256(r.content).hexdigest() != r.headers.get("X-Content-SHA256"):
            raise ValueError("Template package checksum/size mismatch")
        return r.json()


class Pan:
    def __init__(self, config):
        self.config = config
        self.client = httpx.Client(timeout=120, follow_redirects=False)
        self.token = None
        self.expires = 0
        self.last_download = 0
        self.control_lock = threading.RLock()

    def access(self):
        with self.control_lock:
            return self._access()

    def _access(self):
        if self.token and self.expires > time.time():
            return self.token
        r = self.client.post(self.config.pan_base + "/api/v1/access_token", headers={"Platform": "open_platform"}, json={"clientID": self.config.pan_id, "clientSecret": self.config.pan_secret})
        r.raise_for_status()
        data = r.json()
        if data["code"] != 0:
            raise RuntimeError("123Pan authentication failed: " + str(data.get("message")))
        self.token = data["data"]["accessToken"]
        # Conservative cache, never acquire a token per screenshot.
        self.expires = time.time() + 1800
        return self.token

    def download(self, file, path: Path):
        with self.control_lock:
            url = self.download_url(file)
        retrying(lambda: self._download(url, file, path))

    def _download(self, url, file, path: Path):
        digest = hashlib.sha256()
        size = 0
        try:
            # Control credentials never accompany the CDN request.
            with self.client.stream("GET", url, follow_redirects=True) as response, path.open("wb") as dst:
                if response.status_code in (429, 500, 502, 503, 504):
                    raise Retryable(f"download HTTP {response.status_code}")
                response.raise_for_status()
                for block in response.iter_bytes(65536):
                    size += len(block)
                    if size > file["size"] or size > self.config.file_bytes:
                        raise ValueError("Downloaded file exceeds expected size")
                    digest.update(block)
                    dst.write(block)
            if size != file["size"] or digest.hexdigest() != file["sha256"]:
                raise ValueError("Downloaded file hash/size mismatch")
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def download_url(self, file):
        wait = 0.25 - (time.monotonic() - self.last_download)
        if wait > 0:
            time.sleep(wait)
        self.last_download = time.monotonic()
        r = self.client.get(self.config.pan_base + "/api/v1/file/download_info", headers={"Platform": "open_platform", "Authorization": "Bearer " + self.access()}, params={"fileId": file["file_id"]})
        if r.status_code == 401:
            self.token = None
        r.raise_for_status()
        data = r.json()
        if data["code"] == 401:
            self.token = None
        if data["code"] != 0:
            raise RuntimeError("123Pan download: " + str(data.get("message")))
        url = data["data"]["downloadUrl"]
        if urlparse(url).scheme != "https":
            raise ValueError("123Pan returned a non-HTTPS download URL")
        return url


class WebDAV:
    """A stores immutable originals as <sha256>.png in this WebDAV directory."""

    def __init__(self, config):
        self.config = config
        self.base_url = config.webdav_url.rstrip("/") + "/"
        self.origin = httpx.URL(self.base_url)
        self.auth = httpx.BasicAuth(config.webdav_username, config.webdav_password)
        self.client = httpx.Client(timeout=httpx.Timeout(120, connect=10), follow_redirects=False)

    def download(self, file, path: Path):
        if not re.fullmatch(r"[0-9a-f]{64}", file["sha256"]):
            raise ValueError("Invalid original hash")
        retrying(lambda: self._download(self.base_url + file["sha256"] + ".png", file, path))

    def _download(self, url, file, path: Path):
        digest = hashlib.sha256()
        size = 0
        try:
            for _ in range(6):
                target = httpx.URL(url)
                if target.scheme != "https" or target.userinfo:
                    raise ValueError("WebDAV redirect must use HTTPS without URL credentials")
                same_origin = (target.scheme, target.host, target.port) == (self.origin.scheme, self.origin.host, self.origin.port)
                with self.client.stream("GET", target, auth=self.auth if same_origin else None) as response:
                    if response.is_redirect:
                        location = response.headers.get("Location")
                        if not location:
                            raise ValueError("WebDAV redirect has no location")
                        url = urljoin(str(target), location)
                        continue
                    if response.status_code in (429, 502, 503, 504):
                        raise Retryable(f"WebDAV download HTTP {response.status_code}")
                    if response.status_code != 200:
                        # Signed CDN query strings and account credentials must
                        # never become part of a persisted job error/traceback.
                        raise RuntimeError(f"WebDAV download HTTP {response.status_code}")
                    with path.open("wb") as dst:
                        for block in response.iter_bytes(65536):
                            size += len(block)
                            if size > file["size"] or size > self.config.file_bytes:
                                raise ValueError("Downloaded file exceeds expected size")
                            digest.update(block)
                            dst.write(block)
                    if size != file["size"] or digest.hexdigest() != file["sha256"]:
                        raise ValueError("Downloaded file hash/size mismatch")
                    return
            raise ValueError("Too many WebDAV redirects")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
