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
