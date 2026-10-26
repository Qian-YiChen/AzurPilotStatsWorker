from dataclasses import dataclass
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

    def __post_init__(self):
        if self.file_bytes < 8 or self.group_bytes < self.file_bytes:
            raise ValueError("Invalid file/group limits")

    @classmethod
    def load(cls):
        required = ["WORKER_TOKEN", "STATISTICS_URL", "STATISTICS_TOKEN", "PAN_CLIENT_ID", "PAN_CLIENT_SECRET", "ALAS_ROOT"]
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))
        return cls(Path(os.getenv("DATA_DIR", "data")).resolve(), os.environ["WORKER_TOKEN"], os.environ["STATISTICS_URL"].rstrip("/"), os.environ["STATISTICS_TOKEN"], os.getenv("WORKER_ID", "recognizer-1"), os.environ["PAN_CLIENT_ID"], os.environ["PAN_CLIENT_SECRET"], os.getenv("PAN_API_BASE", "https://open-api.123pan.com"), Path(os.environ["ALAS_ROOT"]).resolve(), int(os.getenv("MAX_FILE_BYTES", str(10 * 1024**2))), int(os.getenv("MAX_GROUP_BYTES", str(500 * 1024**2))))
