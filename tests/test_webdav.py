from dataclasses import replace
import hashlib

import httpx
import pytest

from test_worker import config
from worker.clients import WebDAV
from worker.config import Config


def test_webdav_download_uses_hash_filename_and_strips_cdn_credentials(tmp_path):
    data = b"original screenshot bytes"
    sha = hashlib.sha256(data).hexdigest()
    file = {"file_id": 123, "size": len(data), "sha256": sha}
    settings = replace(config(tmp_path), webdav_url="https://dav.invalid/webdav",
                       webdav_username="test-user", webdav_password="test-secret")
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.host == "dav.invalid":
            assert request.url.path == f"/webdav/{file['sha256']}.png"
            assert request.headers["Authorization"].startswith("Basic ")
            return httpx.Response(302, headers={"Location": "https://cdn.invalid/original?signature=private"})
        assert request.url.host == "cdn.invalid"
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=data)

    storage = WebDAV(settings)
    storage.client.close()
    storage.client = httpx.Client(transport=httpx.MockTransport(handle))
    path = tmp_path / "original.png"
    try:
        storage.download(file, path)
        assert path.read_bytes() == data
        assert len(calls) == 2  # No listing, token, or OpenAPI download_info call.
        file["sha256"] = "a" * 64
        with pytest.raises(ValueError, match="mismatch"):
            storage.download(file, path)
        assert not path.exists()
        file["sha256"] = sha
        file["size"] = len(data) - 1
        with pytest.raises(ValueError, match="exceeds"):
            storage.download(file, path)
        assert not path.exists()
    finally:
        storage.client.close()


@pytest.mark.parametrize("target", ["http://cdn.invalid/plain", "https://user:secret@cdn.invalid/original"])
def test_webdav_rejects_unsafe_redirects_without_leaving_originals(tmp_path, target):
    settings = replace(config(tmp_path), webdav_url="https://dav.invalid/webdav",
                       webdav_username="test-user", webdav_password="test-secret")
    storage = WebDAV(settings)
    storage.client.close()
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": target})

    storage.client = httpx.Client(transport=httpx.MockTransport(handle))
    path = tmp_path / "original.png"
    try:
        with pytest.raises(ValueError, match="HTTPS"):
            storage.download({"sha256": "a" * 64, "size": 8}, path)
        assert len(calls) == 1
        assert not path.exists()
    finally:
        storage.client.close()


def test_webdav_configuration_does_not_require_pan_openapi_credentials(tmp_path, monkeypatch):
    for key in ("PAN_CLIENT_ID", "PAN_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"WORKER_TOKEN": "worker", "STATISTICS_URL": "https://c.invalid",
                       "STATISTICS_TOKEN": "stats", "ALAS_ROOT": str(tmp_path),
                       "WEBDAV_URL": "https://dav.invalid/webdav/", "WEBDAV_USERNAME": "dav-user",
                       "WEBDAV_PASSWORD": "dav-password"}.items():
        monkeypatch.setenv(key, value)
    settings = Config.load()
    assert settings.webdav_url == "https://dav.invalid/webdav"
    assert settings.pan_id == settings.pan_secret == ""
    assert "dav-password" not in repr(settings)
