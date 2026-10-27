"""Only JSON, small candidate crops and progress are durable; originals are not."""
import hashlib
import json
import re
import sqlite3
import threading
import time


HEX = re.compile(r"^[0-9a-f]{64}$")


def validate_source(source, file_bytes=10 * 1024**2, group_bytes=500 * 1024**2):
    files = source["files"]
    meta = source["metadata"]
    if not HEX.fullmatch(source["sha256"]) or not 1 <= len(files) <= 1000 or len(files) != len(meta["file_sizes"]):
        raise ValueError("Invalid sample identity/count")
    total = 0
    digest = hashlib.sha256()
    for index, f in enumerate(files):
        if f["index"] != index or not HEX.fullmatch(f["sha256"]) or not 0 < f["size"] <= file_bytes or f["file_id"] <= 0 or f["size"] != meta["file_sizes"][index]:
            raise ValueError("Invalid source file")
        digest.update(bytes.fromhex(f["sha256"]))
        total += f["size"]
    if total > group_bytes or digest.hexdigest() != source["sha256"]:
        raise ValueError("Sample hash/size mismatch")
    if meta["project"] not in ("research", "drop"):
        raise ValueError("Unsupported project")


class Database:
    def __init__(self, path, file_bytes=10 * 1024**2, group_bytes=500 * 1024**2):
        self.file_bytes, self.group_bytes = file_bytes, group_bytes
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,sha TEXT NOT NULL,task TEXT NOT NULL,source TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',result TEXT,candidates TEXT,retry_at REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',UNIQUE(sha,task));
        CREATE TABLE IF NOT EXISTS commands(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',result TEXT,reported INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        UPDATE jobs SET state='pending' WHERE state='processing';
        UPDATE commands SET state='pending' WHERE state='processing';
        """)

    def close(self):
        with self.lock:
            self.db.close()

    def accept(self, batch):
        if batch.get("type") != "batch_ready" or batch.get("protocol_version") != 1 or not re.fullmatch(r"[0-9a-f]{32}", batch.get("batch_id", "")) or not 1 <= len(batch.get("items", [])) <= 30:
            raise ValueError("Invalid batch")
        for source in batch["items"]:
            validate_source(source, self.file_bytes, self.group_bytes)
        payload = json.dumps(batch, separators=(",", ":"), sort_keys=True)
        with self.lock, self.db:
            previous = self.db.execute("SELECT payload FROM batches WHERE id=?", (batch["batch_id"],)).fetchone()
            if previous and previous[0] != payload:
                raise ValueError("Batch ID conflict")
            self.db.execute("INSERT OR IGNORE INTO batches VALUES(?,?)", (batch["batch_id"], payload))
            for source in batch["items"]:
                self.db.execute("INSERT OR IGNORE INTO jobs(sha,task,source) VALUES(?,'ingest',?)", (source["sha256"], json.dumps(source)))

    def claim(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM jobs WHERE state IN ('pending','delivery') AND retry_at<=? ORDER BY CASE WHEN task LIKE 'command:%' THEN 0 ELSE 1 END,id LIMIT 1", (time.time(),)).fetchone()
            if row and row["state"] == "pending":
                self.db.execute("UPDATE jobs SET state='processing' WHERE id=?", (row["id"],))
            return dict(row) if row else None

    def save_result(self, job, result, candidates):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET state='delivery',result=?,candidates=?,error='' WHERE id=?", (json.dumps(result), json.dumps(candidates), job["id"]))

    def finish(self, job):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET state='done',candidates=NULL,error='' WHERE id=?", (job["id"],))

    def retry(self, job, error):
        with self.lock, self.db:
            state = "delivery" if self.db.execute("SELECT result FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] else "pending"
            self.db.execute("UPDATE jobs SET state=?,retry_at=?,error=? WHERE id=?", (state, time.time() + 10, str(error)[:2000], job["id"]))

    def quarantine(self, job, message):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET state='failed',error=? WHERE id=?", (message, job["id"]))

    def pending(self):
        with self.lock:
            return self.db.execute("SELECT count(*) FROM jobs WHERE state NOT IN ('done','failed')").fetchone()[0]

    def is_redundant_research_list(self, source):
        """Only discard mislabeled completion frames already submitted as lists."""
        meta = source["metadata"]
        if meta.get("project") != "research" or meta.get("scene") != "research_complete":
            return False
        wanted = {file["sha256"] for file in source["files"]}
        covered = set()
        with self.lock:
            rows = self.db.execute("SELECT source FROM jobs WHERE task='ingest' AND sha<>?", (source["sha256"],))
            for row in rows:
                other = json.loads(row[0])
                other_meta = other["metadata"]
                if (other_meta.get("project") == "research" and other_meta.get("scene") == "research_refresh"
                        and other_meta.get("device_id") == meta.get("device_id")
                        and other_meta.get("instance_id") == meta.get("instance_id")):
                    covered.update(file["sha256"] for file in other["files"])
                    if wanted <= covered:
                        return True
        return False

    def setting(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def release(self):
        with self.lock:
            return self.setting("release_id", 0), self.setting("templates", [])

    def set_release(self, id_, templates):
        with self.lock, self.db:
            for key, value in (("release_id", id_), ("templates", templates)):
                self.db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def update_reprocess_command(self, id_):
        with self.lock, self.db:
            counts = dict(self.db.execute("SELECT state,count(*) FROM jobs WHERE task=? GROUP BY state", (f"command:{id_}",)))
            total = sum(counts.values())
            finished = counts.get("done", 0) + counts.get("failed", 0)
            state = ("failed" if counts.get("failed") else "done") if total == finished else "running"
            self.db.execute("UPDATE commands SET state=?,result=?,reported=0 WHERE id=?", (state, json.dumps({"total": total, "finished": finished, "counts": counts}), id_))

    def sync_updates(self):
        with self.lock:
            return [{"id": r["id"], "state": r["state"], "result": json.loads(r["result"] or "{}")} for r in self.db.execute("SELECT * FROM commands WHERE state IN ('running','done','failed') AND reported=0 LIMIT 100")]

    def accept_commands(self, commands, reported):
        with self.lock, self.db:
            for cmd in commands:
                self.db.execute("INSERT OR IGNORE INTO commands(id,payload) VALUES(?,?)", (cmd["id"], json.dumps(cmd)))
            for update in reported:
                if update["state"] in ("done", "failed"):
                    self.db.execute("UPDATE commands SET reported=1 WHERE id=?", (update["id"],))

    def command(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM commands WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
            if row:
                self.db.execute("UPDATE commands SET state='running' WHERE id=?", (row["id"],))
            return dict(row) if row else None

    def command_result(self, id_, state, result):
        with self.lock, self.db:
            self.db.execute("UPDATE commands SET state=?,result=?,reported=0 WHERE id=?", (state, json.dumps(result), id_))

    def enqueue_reprocess(self, cmd):
        with self.lock, self.db:
            for source in cmd["payload"]["sources"]:
                validate_source(source, self.file_bytes, self.group_bytes)
                self.db.execute("INSERT OR IGNORE INTO jobs(sha,task,source) VALUES(?,?,?)", (source["sha256"], f"command:{cmd['id']}", json.dumps(source)))

    def enqueue_release(self, source, release):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(sha,task,source) VALUES(?,?,?)", (source["sha256"], f"release:{release}", json.dumps(source)))
