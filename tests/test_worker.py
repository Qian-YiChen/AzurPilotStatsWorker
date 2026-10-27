import hashlib
import json

from fastapi.testclient import TestClient
import pytest
import httpx

from worker.app import create_app
from worker.config import Config
from worker.db import Database, validate_source
from worker.engine import Engine
from worker.clients import Pan


def source(index=0):
    file_hash = hashlib.sha256(str(index).encode()).hexdigest()
    sample_hash = hashlib.sha256(bytes.fromhex(file_hash)).hexdigest()
    return {"sha256": sample_hash, "metadata": {"upload_id": "00000000-0000-4000-8000-000000000000", "device_id": "a" * 32, "instance_id": "b" * 16, "project": "drop", "scene": "campaign", "server": "cn", "collected_at": "2026-10-26T12:00:00Z", "file_sizes": [8]}, "files": [{"index": 0, "file_id": index + 1, "sha256": file_hash, "size": 8}]}


def batch(count=30):
    return {"type": "batch_ready", "protocol_version": 1, "batch_id": "a" * 32, "items": [source(i) for i in range(count)]}


def config(tmp_path):
    return Config(tmp_path, "worker-secret", "http://unused", "statistics-secret", "test-worker", "pan-id", "pan-secret", "https://unused", tmp_path)


def test_ack_requires_auth_and_durable_acceptance(tmp_path):
    path = tmp_path / "state.sqlite"
    with TestClient(create_app(config(tmp_path), start_engine=False)) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/internal/v1/batches"):
                pass
        with client.websocket_connect("/internal/v1/batches", headers={"Authorization": "Bearer worker-secret"}) as ws:
            ws.send_json(batch())
            assert ws.receive_json() == {"type": "batch_accepted", "batch_id": "a" * 32}
            ws.send_json(batch())
            assert ws.receive_json()["batch_id"] == "a" * 32
        assert client.get("/healthz").json()["pending"] == 30
    db = Database(path)
    assert db.pending() == 30
    assert db.claim()["state"] == "pending"
    db.close()
    db = Database(path)
    assert db.claim() is not None  # Interrupted processing was recovered.
    db.close()


def test_batch_conflict_and_forged_hash(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    data = batch(1)
    db.accept(data)
    data["items"][0]["metadata"]["scene"] = "different"
    with pytest.raises(ValueError, match="conflict"):
        db.accept(data)
    forged = source()
    forged["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="mismatch"):
        validate_source(forged)
    assert db.pending() == 1
    db.close()


def test_result_survives_delivery_failure_without_recognition(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    db.accept(batch(1))
    engine = Engine.__new__(Engine)
    engine.db = db
    calls = []

    class Stats:
        offline = True

        def revision(self, sha):
            return 0

        def post(self, path, data):
            if self.offline:
                raise RuntimeError("network down")
            return {"receipts": [{"status": "accepted"}]}

    engine.stats = Stats()

    def recognize(sample, templates):
        calls.append(sample["sha256"])
        return {"status": "complete", "events": [{"event_type": "drop", "items": [{"item": "Coins", "amount": 2, "tag": ""}]}], "diagnostics": [], "candidates": [], "used_templates": []}

    engine.recognize = recognize
    first = db.claim()
    engine.process(first)
    with db.db:
        db.db.execute("UPDATE jobs SET retry_at=0")
    second = db.claim()
    assert second["state"] == "delivery"
    assert json.loads(second["result"])["revision"] == 1
    engine.stats.offline = False
    engine.process(second)
    assert len(calls) == 1
    assert db.pending() == 0
    db.close()


def test_work_files_removed_even_when_recognizer_fails(tmp_path, monkeypatch):
    db = Database(tmp_path / "state.sqlite")
    engine = Engine(config(tmp_path), db)

    class Pan:
        def download(self, file, path):
            path.write_bytes(b"original")

    engine.pan = Pan()
    monkeypatch.setattr(engine.recognizers, 'recognize', lambda *a, **k: (_ for _ in ()).throw(RuntimeError("recognizer crashed")))
    with pytest.raises(RuntimeError):
        engine.recognize(source())
    assert list(engine.work.iterdir()) == []
    engine.executor.shutdown()
    engine.stats.client.close()
    db.close()


def test_atomic_release_snapshot_and_command_progress(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    db.set_release(3, [{"id": "version-3"}])
    assert db.release() == (3, [{"id": "version-3"}])
    cmd = {"id": 1, "action": "reprocess", "payload": {"sources": [source()], "templates": []}}
    db.accept_commands([cmd], [])
    db.command()
    db.enqueue_reprocess(cmd)
    db.update_reprocess_command(1)
    assert db.sync_updates()[0]["state"] == "running"
    row = db.claim()
    db.finish(row)
    db.update_reprocess_command(1)
    update = db.sync_updates()[0]
    assert update["state"] == "done" and update["result"]["finished"] == 1
    db.accept_commands([], [update])
    assert not db.sync_updates()
    db.close()


def test_publication_during_recognition_does_not_miss_source(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    db.accept(batch(1))
    engine = Engine.__new__(Engine)
    engine.db = db

    class Stats:
        def revision(self, sha):
            return 0

        def post(self, path, body):
            return {"receipts": [{"status": "accepted"}]}

    engine.stats = Stats()

    def recognize(sample, templates):
        db.set_release(1, [])  # C publishes before learning about this source.
        return {"status": "needs_review", "events": [], "diagnostics": ["missing_template"], "candidates": [], "used_templates": []}

    engine.recognize = recognize
    engine.process(db.claim())
    retry = db.claim()
    assert retry["task"] == "release:1" and retry["sha"] == source()["sha256"]
    db.close()


def test_cloud_download_checks_hash_and_keeps_credentials_off_cdn(tmp_path):
    data = b"a valid original"
    file = {"file_id": 123, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    path = tmp_path / "original.png"

    def handle(request):
        if request.url.path == "/api/v1/access_token":
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "pan-secret"}})
        if request.url.path == "/api/v1/file/download_info":
            assert request.headers["Authorization"] == "Bearer pan-secret"
            assert request.url.params["fileId"] == "123"
            return httpx.Response(200, json={"code": 0, "data": {"downloadUrl": "https://cdn.invalid/image"}})
        assert request.url.host == "cdn.invalid" and "Authorization" not in request.headers
        return httpx.Response(200, content=data)

    pan = Pan(config(tmp_path))
    pan.client.close()
    pan.client = httpx.Client(transport=httpx.MockTransport(handle))
    try:
        pan.download(file, path)
        assert path.read_bytes() == data
        file["sha256"] = "a" * 64
        with pytest.raises(ValueError, match="mismatch"):
            pan.download(file, path)
        assert not path.exists()
    finally:
        pan.client.close()


def test_configured_file_and_group_limits(tmp_path):
    with pytest.raises(ValueError, match="source file"):
        validate_source(source(), file_bytes=7, group_bytes=16)
    with pytest.raises(ValueError, match="mismatch"):
        validate_source(source(), file_bytes=8, group_bytes=7)
    db = Database(tmp_path / "state.sqlite", file_bytes=8, group_bytes=8)
    db.accept(batch(1))
    assert db.pending() == 1
    db.close()
