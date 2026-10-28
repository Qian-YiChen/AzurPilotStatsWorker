import json
from types import SimpleNamespace

from PIL import Image
import pytest

from worker.commission import recognize_commissions, resolve_name
from worker.db import Database
from worker.engine import Engine
from worker.recognize import frames
from test_worker import batch


def item(name="Coins", amount=100, known=True, tag=""):
    return SimpleNamespace(name=name, amount=amount, tag=tag, is_known_item=lambda: known)


class Reader:
    def classify(self, image):
        return image["kind"]

    def read_name(self, image):
        if image.get("error"):
            raise ValueError("unreadable name")
        return image["name"]

    def read_items(self, image):
        if image.get("error"):
            raise ValueError("unreadable amount")
        return image["items"]


def success(name="小型商船护卫", **kwargs):
    return {"kind": "success", "name": name, **kwargs}


def reward(items=None, **kwargs):
    return {"kind": "reward", "items": [item()] if items is None else items, **kwargs}


def parse(images):
    return recognize_commissions(((i, 0, image) for i, image in enumerate(images)), Reader(), set(), [])


@pytest.mark.parametrize("raw,server,expected", [
    (" 日常资源开发 Ⅲ ", "cn_ios", "日常资源开发III"),
    ("Small Merchant Escort", "en", "小型商船护卫"),
    ("小型商船護衛", "tw", "小型商船护卫"),
    ("小型船団護衛", "jp", "小型商船护卫"),
])
def test_names_use_existing_language_aliases_and_normalize_suffixes(raw, server, expected):
    assert resolve_name(raw, server) == expected


def test_uncertain_name_is_rejected_and_suffix_can_recover_missing_numerals():
    with pytest.raises(ValueError):
        resolve_name("unrecognized commission", "en")
    assert resolve_name("日常资源开发", "cn", "III") == "日常资源开发III"


def test_conflicting_numeral_readings_and_shared_aliases_are_not_guessed():
    with pytest.raises(ValueError):
        resolve_name("日常资源开发I", "cn", "II")
    with pytest.raises(ValueError):
        resolve_name("BIW要人護衛", "jp")


def test_identical_complete_records_are_deduplicated_within_a_group():
    result = parse([success(), reward(), success(), reward(), {"kind": "exit"}])
    assert result["status"] == "complete"
    assert len(result["events"]) == 1
    assert result["events"][0]["items"][0]["amount"] == 100
    assert all(event["project"] == "小型商船护卫" for event in result["events"])
    assert all(event["duration_seconds"] == 0 for event in result["events"])
    assert any(entry["reason"] == "duplicate_commission" for entry in result["skipped_frames"])


@pytest.mark.parametrize("next_success,next_items", [
    (success("中型商船护卫"), [item()]),
    (success(kind="perfect"), [item()]),
    (success(), [item(amount=101)]),
    (success(), [item(tag="bonus")]),
    (success(), [item(name="Oil")]),
])
def test_different_name_success_state_or_reward_labels_remain_separate(next_success, next_items):
    result = parse([success(), reward(), next_success, reward(next_items)])
    assert len(result["events"]) == 2


def test_deduplication_is_independent_of_item_order_and_request_history():
    images = [success(), reward([item(), item("Oil", 2)]),
              success(), reward([item("Oil", 2), item()])]
    assert len(parse(images)["events"]) == 1
    assert len(parse(images)["events"]) == 1


def test_missing_reward_does_not_shift_later_pairing():
    result = parse([success("first"), success("second"), reward(), success("third"), reward()])
    assert [event["project"] for event in result["events"]] == ["second", "third"]
    assert result["skipped_frames"][0]["file_index"] == 0


@pytest.mark.parametrize("boundary", [None, {"kind": "unknown"}])
def test_unreadable_boundary_cannot_attach_later_reward_to_old_name(boundary):
    result = parse([success("old"), boundary, reward(), success("new"), reward()])
    assert [event["project"] for event in result["events"]] == ["new"]
    assert any(entry["reason"] == "reward_without_commission" for entry in result["skipped_frames"])


def test_bad_name_or_reward_only_discards_its_own_commission():
    result = parse([success(error=True), reward(), success("bad amount"), reward(error=True),
                    success("good"), reward()])
    assert [event["project"] for event in result["events"]] == ["good"]
    assert result["diagnostics"] == []
    assert result["candidates"] == []
    assert result["status"] == "complete"
    assert result["skipped_frames"]


@pytest.mark.parametrize("bad_items", [[item(known=False), item()], [item(amount=0)],
                                      [item(amount=1_000_000_001)],
                                      [item(amount=600_000_000), item(amount=600_000_000)], []])
def test_partly_read_reward_is_not_reported_as_a_complete_commission(bad_items):
    result = parse([success("bad"), reward(bad_items), success("good"), reward()])
    assert [event["project"] for event in result["events"]] == ["good"]


def test_orphan_reward_is_invalid_and_does_not_block_a_later_pair():
    result = parse([reward(), success(), reward()])
    assert len(result["events"]) == 1
    assert result["events"][0]["items"][0]["amount"] == 100
    assert result["skipped_frames"][0]["reason"] == "reward_without_commission"


def test_two_rewards_invalidate_the_whole_record_but_not_the_next_pair():
    result = parse([success("bad"), reward(), reward(), success("good"), reward()])
    assert [event["project"] for event in result["events"]] == ["good"]
    assert result["skipped_frames"][0]["reason"] == "invalid_pairing:reward_count:2"
    invalid = parse([success(), reward(), reward()])
    assert invalid["status"] == "invalid" and invalid["events"] == []


def test_invalid_pair_does_not_reserve_a_deduplication_signature():
    result = parse([success(), reward(), reward(), success(), reward()])
    assert len(result["events"]) == 1
    assert not any(entry["reason"] == "duplicate_commission" for entry in result["skipped_frames"])


def test_exit_ends_the_sequence_and_unfinished_tail_is_dropped():
    result = parse([success("good"), reward(), success("unfinished"), {"kind": "exit"},
                    success("after exit"), reward()])
    assert [event["project"] for event in result["events"]] == ["good"]
    assert result["skipped_frames"][-1]["reason"] == "invalid_pairing:reward_count:0"
    assert parse([success()])["status"] == "invalid"


def test_zero_successes_have_no_events_and_keep_failure_diagnostics():
    result = parse([success(error=True), reward()])
    assert result["events"] == []
    assert result["status"] == "needs_review"
    assert "no_complete_commission" in result["diagnostics"]


def test_corrupt_file_and_bad_size_keep_their_sequence_positions(tmp_path):
    good, bad, small, packed = [tmp_path / name for name in ("good.png", "bad.png", "small.png", "packed.png")]
    Image.new("RGB", (1280, 720)).save(good)
    bad.write_bytes(b"broken png")
    Image.new("RGB", (640, 360)).save(small)
    Image.new("RGB", (1280, 1440)).save(packed)
    diagnostics = []
    decoded = list(frames([good, bad, small, packed], diagnostics, recover=True))
    assert [(i, j, image is None) for i, j, image in decoded] == [
        (0, 0, False), (1, 0, True), (2, 0, True), (3, 0, False), (3, 1, False)]
    assert len(diagnostics) == 2
    with pytest.raises(OSError):
        list(frames([bad], []))


def test_partial_success_survives_restart_and_delivery_retry(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    packet = batch(1)
    packet["items"][0]["metadata"]["scene"] = "commission"
    db.accept(packet)
    engine = Engine.__new__(Engine)
    engine.db = db
    parsed = parse([success("first"), reward(), success(error=True), reward(), success("last"), reward()])
    calls = []

    def recognize(*args):
        calls.append(1)
        return parsed

    class Stats:
        offline = True

        def revision(self, sha):
            return 0

        def post(self, path, body):
            result = body["results"][0]
            assert result["status"] == "complete"
            assert result["diagnostics"] == [] and result["candidate_ids"] == []
            assert "skipped_frames" not in result
            assert [event["project"] for event in result["events"]] == ["first", "last"]
            if self.offline:
                raise RuntimeError("offline")
            return {"receipts": [{"status": "accepted"}]}

    engine.recognize = recognize
    engine.stats = Stats()
    engine.process(db.claim())
    db.close()
    engine.db = Database(tmp_path / "state.sqlite")
    with engine.db.db:
        engine.db.db.execute("UPDATE jobs SET retry_at=0")
    job = engine.db.claim()
    assert job["state"] == "delivery"
    assert len(json.loads(job["result"])["events"]) == 2
    engine.stats.offline = False
    engine.process(job)
    assert calls == [1]
    assert engine.db.pending() == 0
    engine.db.close()
