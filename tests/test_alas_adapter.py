"""Optional real OCR smoke test; no captured game screenshots are committed."""
import json
import os
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont
import pytest


@pytest.mark.skipif(not os.getenv("ALAS_ROOT") or not os.getenv("ALAS_PYTHON"), reason="Requires the existing Alas OCR environment")
def test_actual_alas_recognizer_and_published_overlay(tmp_path):
    root = Path(os.environ["ALAS_ROOT"])
    screen = Image.open(root / "assets/cn/combat/GET_ITEMS_1.png").convert("RGB")
    item = Image.open(root / "assets/stats/research_items/BlueprintHalford.png").convert("RGB")
    draw = ImageDraw.Draw(item)
    draw.rectangle((60, 71, 91, 95), fill="black")
    draw.text((80, 71), "1", font=ImageFont.load_default(size=20), fill="white")
    screen.paste(item, (400, 298))
    path = tmp_path / "sample.png"
    screen.save(path)
    request = {"alas_root": str(root), "files": [str(path)], "templates": [], "source": {"sha256": "a" * 64, "metadata": {"project": "drop", "scene": "campaign", "server": "cn"}}}
    adapter = Path(__file__).resolve().parents[1] / "worker/recognize.py"

    def parse():
        inp, out = tmp_path / "input.json", tmp_path / "output.json"
        inp.write_text(json.dumps(request), encoding="utf-8")
        process = subprocess.run([os.environ["ALAS_PYTHON"], str(adapter), str(inp), str(out)], capture_output=True, timeout=180)
        assert process.returncode == 0, process.stderr.decode(errors="replace")
        parsed = json.loads(out.read_text(encoding="utf-8"))
        assert not any("Traceback" in d for d in parsed["diagnostics"]), parsed
        return parsed

    first = parse()
    assert first["status"] == "needs_review" and first["candidates"], first
    candidate = first["candidates"][0]
    request["templates"] = [{"id": candidate["id"], "item_id": "SmokeBlueprint", "scene": "all", "enabled": True, "image": candidate["image"]}]
    second = parse()
    assert second["status"] == "complete", second
    assert second["events"][0]["items"] == [{"item": "SmokeBlueprint", "amount": 1, "tag": ""}]
    assert second["used_templates"] == [candidate["id"]]


@pytest.mark.skipif(not os.getenv("ALAS_ROOT") or not os.getenv("ALAS_PYTHON"), reason="Requires the existing Alas OCR environment")
def test_commission_real_ocr_keeps_complete_pairs_after_failed_frames(tmp_path):
    root = Path(os.environ["ALAS_ROOT"])
    font_paths = [Path("C:/Windows/Fonts/msyh.ttc"), Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]
    font_path = next((path for path in font_paths if path.is_file()), None)
    server = "cn" if font_path else "en"
    font = ImageFont.truetype(str(font_path), 20) if font_path else ImageFont.load_default(size=20)
    names = ["小型商船护卫", "日常资源开发III"] if font_path else ["Small Merchant Escort", "Daily Resource Extraction III"]
    expected = ["小型商船护卫", "日常资源开发III"]
    successes = []
    for index, name in enumerate([*names, "???"]):
        image = Image.open(root / f"assets/{server}/azur_stats/COMMISSION_DONE.png").convert("RGB")
        draw = ImageDraw.Draw(image)
        draw.rectangle((293, 162, 592, 182), fill="black")
        draw.text((305, 162), name, font=font, fill="white", anchor="lt")
        path = tmp_path / f"success_{index}.png"
        image.save(path)
        successes.append(str(path))
    reward = Image.open(root / f"assets/{server}/combat/GET_ITEMS_1.png").convert("RGB")
    icon = Image.open(root / "assets/stats/commission_items/Coins.png").convert("RGB")
    draw = ImageDraw.Draw(icon)
    draw.rectangle((60, 71, 91, 95), fill="black")
    draw.text((80, 71), "1", font=ImageFont.load_default(size=20), fill="white")
    reward.paste(icon, (592, 298))
    reward_path = tmp_path / "reward.png"
    reward.save(reward_path)
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"invalid PNG")
    request = {"alas_root": str(root), "templates": [],
               "source": {"sha256": "a" * 64, "metadata": {"project": "drop", "scene": "commission", "server": server}},
               "files": [successes[0], str(reward_path), successes[0], str(reward_path), str(corrupt),
                         successes[2], str(reward_path), successes[0], str(reward_path), str(reward_path),
                         successes[1], str(reward_path)]}
    inp, out = tmp_path / "request.json", tmp_path / "result.json"
    inp.write_text(json.dumps(request), encoding="utf-8")
    adapter = Path(__file__).resolve().parents[1] / "worker/recognize.py"
    process = subprocess.run([os.environ["ALAS_PYTHON"], str(adapter), str(inp), str(out)], capture_output=True, timeout=180)
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert parsed["status"] == "complete", parsed
    assert [event["project"] for event in parsed["events"]] == expected, parsed
    assert parsed["diagnostics"] == [] and parsed["candidates"] == []
    assert parsed["skipped_frames"]
    assert any(entry["reason"] == "duplicate_commission" for entry in parsed["skipped_frames"])
    assert any(entry["reason"] == "invalid_pairing:reward_count:2" for entry in parsed["skipped_frames"])
    assert all(event["items"] == [{"item": "Coins", "tag": "", "amount": 1}] for event in parsed["events"])
