import copy
import hashlib

from test_worker import batch, source
from worker.db import Database


def test_redundant_list_requires_all_frames_and_same_instance(tmp_path):
    first, second = source(901), source(902)
    for item in (first, second):
        item["metadata"].update(project="research", scene="research_refresh")
    original = copy.deepcopy(first)
    original["metadata"].update(scene="research_complete", file_sizes=[8, 8])
    original["files"].append(copy.deepcopy(second["files"][0]))
    original["files"][1]["index"] = 1
    original["sha256"] = hashlib.sha256(b"".join(bytes.fromhex(f["sha256"]) for f in original["files"])).hexdigest()
    db = Database(tmp_path / "state.sqlite")
    try:
        packet = batch(1)
        packet["items"] = [original, first]
        db.accept(packet)
        assert not db.is_redundant_research_list(original)
        packet["batch_id"] = "b" * 32
        packet["items"] = [second]
        db.accept(packet)
        assert db.is_redundant_research_list(original)
        original["metadata"]["instance_id"] = "c" * 16
        assert not db.is_redundant_research_list(original)
    finally:
        db.close()


def test_manual_reprocess_precedes_existing_ingest_backlog(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    try:
        db.accept(batch(3))
        db.enqueue_reprocess({"id": 42, "payload": {"sources": [source(900)]}})
        assert db.claim()["task"] == "command:42"
        assert db.claim()["task"] == "ingest"
    finally:
        db.close()
