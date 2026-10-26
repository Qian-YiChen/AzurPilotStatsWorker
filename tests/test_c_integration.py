"""Explicitly opt in against a disposable, already running C test database."""
import hashlib
import json
import os
from pathlib import Path
import shutil

import httpx
from PIL import Image, ImageDraw, ImageFont
import pytest

from worker.config import Config
from worker.db import Database
from worker.engine import Engine


@pytest.mark.skipif(not all(os.getenv(k) for k in ("STATISTICS_TEST_URL", "STATISTICS_TEST_TOKEN", "STATISTICS_TEST_ADMIN", "ALAS_ROOT", "ALAS_PYTHON")), reason="Requires disposable C and real OCR environment")
def test_real_ocr_candidate_publish_reprocess_and_c_statistics(tmp_path):
    root = Path(os.environ["ALAS_ROOT"])
    image = Image.open(root / "assets/cn/combat/GET_ITEMS_1.png").convert("RGB")
    item = Image.open(root / "assets/stats/research_items/BlueprintHalford.png").convert("RGB")
    draw = ImageDraw.Draw(item)
    draw.rectangle((60, 71, 91, 95), fill="black")
    draw.text((80, 71), "1", font=ImageFont.load_default(size=20), fill="white")
    image.paste(item, (400, 298))
    original = tmp_path / "source.png"
    image.save(original)
    file_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    sha = hashlib.sha256(bytes.fromhex(file_hash)).hexdigest()
    size = original.stat().st_size
    source = {"sha256": sha, "metadata": {"upload_id": "303d1f40-3280-4053-84dd-574a83f59d20", "device_id": "a"*32, "instance_id": "b"*16, "project": "drop", "scene": "integration_smoke", "server": "cn", "collected_at": "2026-10-26T10:00:00Z", "file_sizes": [size]}, "files": [{"index": 0, "size": size, "file_id": 123, "sha256": file_hash}]}
    config = Config(tmp_path, "test-worker", os.environ["STATISTICS_TEST_URL"], os.environ["STATISTICS_TEST_TOKEN"], "integration-worker", "unused", "unused", "https://unused", root)
    db = Database(tmp_path / "state.sqlite")
    engine = Engine(config, db)
    cloud = engine.pan

    class FakeCloud:
        def download(self, file, path):
            shutil.copyfile(original, path)

    engine.pan = FakeCloud()
    admin = httpx.Client(base_url=config.c_url, headers={"Authorization": "Bearer " + os.environ["STATISTICS_TEST_ADMIN"]}, timeout=60)
    try:
        assert engine.stats.revision(sha) == 0, "Use a fresh dedicated test database for this opt-in test"
        db.accept({"type": "batch_ready", "protocol_version": 1, "batch_id": "a"*32, "items": [source]})
        engine.process(db.claim())
        current = engine.stats.client.get("/internal/v1/statistics/results/" + sha).json()
        assert current["status"] == "needs_review", current
        candidates = admin.get("/api/v1/admin/template-candidates?status=pending").json()["items"]
        candidate = next(c for c in candidates if any(s["sample_sha256"] == sha for s in c["sources"]))
        template = {"id": candidate["id"], "item_id": "IntegrationBlueprint", "name": "联调验证图纸", "category": "ship_blueprint", "rarity": 4, "series": 6, "scene": "drop", "enabled": True, "image": candidate["image"]}
        response = admin.patch("/api/v1/admin/template-candidates/" + candidate["id"], json={"status": "assigned", "template": template})
        response.raise_for_status()
        response = admin.post("/api/v1/admin/templates/publish")
        response.raise_for_status()
        release = response.json()["release_id"]
        response = admin.post("/api/v1/admin/statistics/reprocess", json={"ids": [sha]})
        response.raise_for_status()
        command = response.json()["command_id"]
        engine.synchronize()
        assert db.release()[0] == release
        row = dict(db.db.execute("SELECT * FROM commands WHERE id=?", (command,)).fetchone())
        engine.control(row)
        engine.process(db.claim())
        receipt = engine.stats.client.get("/internal/v1/statistics/results/" + sha).json()
        assert receipt["revision"] == 2 and receipt["status"] == "complete", receipt
        row = dict(db.db.execute("SELECT * FROM jobs WHERE task=?", (f"command:{command}",)).fetchone())
        row["state"] = "delivery"
        engine.process(row)  # Replay the exact structured result, including revision.
        response = engine.stats.client.get("/api/v2/get/drop", params={"scene": "integration_smoke", "from": "2026-10-26T00:00:00Z", "to": "2026-10-27T00:00:00Z"})
        response.raise_for_status()
        summary = response.json()
        assert summary["groups"][0]["samples"] == 1 and summary["items"][0]["amount"] == 1, summary
        assert not list(engine.work.iterdir())
    finally:
        engine.executor.shutdown()
        engine.stats.client.close()
        cloud.client.close()
        admin.close()
        db.close()
