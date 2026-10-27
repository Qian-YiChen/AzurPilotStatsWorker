from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass(frozen=True)
class Config:
    data: Path
    worker_token: str
    c_url: str
    c_token: str
    worker_id: str
    pan_id: str
    pan_secret: str
    pan_base: str
    alas_root: Path
    file_bytes: int = 10 * 1024**2
    group_bytes: int = 500 * 1024**2
    uploader_url: str = ""
    webdav_url: str = ""
    webdav_username: str = field(default="", repr=False)
    webdav_password: str = field(default="", repr=False)

    def __post_init__(self):
        if self.file_bytes < 8 or self.group_bytes < self.file_bytes:
            raise ValueError("Invalid file/group limits")
        if self.uploader_url:
            from urllib.parse import urlparse
            url = urlparse(self.uploader_url)
            if url.scheme not in ("ws", "wss") or not url.hostname or url.username or url.password:
                raise ValueError("UPLOADER_WS_URL requires ws:// or wss:// without embedded credentials")
        if self.webdav_url:
            from urllib.parse import urlparse
            url = urlparse(self.webdav_url)
            if (url.scheme != "https" or not url.hostname or url.username or url.password
                    or url.query or url.fragment or not self.webdav_username or not self.webdav_password):
                raise ValueError("WebDAV requires an HTTPS directory URL and separate username/password")

    @classmethod
    def load(cls):
        required = ["WORKER_TOKEN", "STATISTICS_URL", "STATISTICS_TOKEN", "ALAS_ROOT"]
        required += ["WEBDAV_USERNAME", "WEBDAV_PASSWORD"] if os.getenv("WEBDAV_URL") else ["PAN_CLIENT_ID", "PAN_CLIENT_SECRET"]
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))
        return cls(Path(os.getenv("DATA_DIR", "data")).resolve(), os.environ["WORKER_TOKEN"], os.environ["STATISTICS_URL"].rstrip("/"), os.environ["STATISTICS_TOKEN"], os.getenv("WORKER_ID", "recognizer-1"), os.getenv("PAN_CLIENT_ID", ""), os.getenv("PAN_CLIENT_SECRET", ""), os.getenv("PAN_API_BASE", "https://open-api.123pan.com"), Path(os.environ["ALAS_ROOT"]).resolve(), int(os.getenv("MAX_FILE_BYTES", str(10 * 1024**2))), int(os.getenv("MAX_GROUP_BYTES", str(500 * 1024**2))), os.getenv("UPLOADER_WS_URL", ""), os.getenv("WEBDAV_URL", "").rstrip("/"), os.getenv("WEBDAV_USERNAME", ""), os.getenv("WEBDAV_PASSWORD", ""))
