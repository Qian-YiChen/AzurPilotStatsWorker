"""Runs in ALAS_PYTHON, isolating the existing OCR runtime from FastAPI.

Input/output JSON files are private work files; original PNGs are removed by
the supervisor in a finally block. No game/device automation is started.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback


def recognize(request):
    root = Path(request["alas_root"]).resolve()
    sys.path.insert(0, str(root))
    os.chdir(root)
    import cv2
    import numpy as np
    from PIL import Image
    import module.config.server as server
    server.server = "cn" if request["source"]["metadata"]["server"].startswith("cn") else request["source"]["metadata"]["server"]
    from module.statistics.get_items import GetItemsStatistics
    from module.statistics.item import ItemGrid
    from module.statistics.research_drop import ResearchDropParser

    source = request["source"]
    meta = source["metadata"]
    research = meta["project"] == "research"
    parser = ResearchDropParser() if research else None
    if parser:
        stats = parser.stats
    else:
        stats = GetItemsStatistics()
        stats.grid = ItemGrid(None, {})
        if meta["scene"].startswith("opsi_"):
            stats.load_template_folder("./assets/stats/opsi_items")
            stats.load_template_folder("./assets/stats/opsi_reward_items")
        elif meta["scene"].startswith("commission"):
            stats.load_template_folder("./assets/stats/commission_items")
        else:
            stats.load_template_folder("./assets/stats/battle_items")
    grid = stats._target_grid()
    overlay = {}
    for t in request["templates"]:
        if not t["enabled"] or t["scene"] not in (meta["project"], "all"):
            continue
        raw = base64.b64decode(t["image"], validate=True)
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Invalid published template")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        key = t["item_id"] + "_" + str(int(t["id"][:12], 16))
        overlay[key] = t["id"]
        grid.templates[key] = image
        grid.colors[key] = cv2.mean(image)[:3]
        grid.templates_hit[key] = 0
    used = set()
    match = grid.match_template

    def tracked_match(image, *args, **kwargs):
        key = match(image, *args, **kwargs)
        if key in overlay:
            used.add(overlay[key])
        return key

    grid.match_template = tracked_match
    candidates = []
    diagnostics = []
    events = []
    # Repeated reward pages can overlap. The upstream merger identifies the
    # repeated item by name, amount and reward tag within one reward event.
    rewards = {}
    project = ""
    series = 0
    reward_frames = 0
    from module.research.assets import QUEUE_CHECK
    from module.research.project_data import LIST_RESEARCH_PROJECT
    catalog = {(x["series"], x["name"]): x for x in LIST_RESEARCH_PROJECT}

    for file_index, path in enumerate(request["files"]):
        with Image.open(path) as png:
            if png.format != "PNG" or png.width != 1280 or png.height % 720 or not 1 <= png.height // 720 <= 10:
                diagnostics.append(f"file:{file_index}:unsupported_resolution")
                continue
            image = np.asarray(png.convert("RGB"))
        for frame_index in range(image.shape[0] // 720):
            frame = image[frame_index * 720:(frame_index + 1) * 720]
            if research and meta["scene"] == "research_refresh":
                from module.research.project import get_research_name, get_research_series
                names = get_research_name(frame)
                series_list = get_research_series(frame)
                if len(names) != 5 or len(series_list) != 5 or any((int(s), n) not in catalog for s, n in zip(series_list, names)):
                    diagnostics.append("research_refresh:incomplete_project_list")
                else:
                    for s, n in zip(series_list, names):
                        events.append({"event_type": "refresh", "series": int(s), "project": n, "focus_series": 0, "duration_seconds": 0, "items": []})
                continue
            if research and QUEUE_CHECK.match(frame, offset=(10, 10)):
                project = parser._read_project(frame)
                series = parser._read_series(frame)
                continue
            try:
                found = stats.stats_get_items(frame, tag=True)
            except Exception as exc:
                if type(exc).__name__ != "ImageError":
                    diagnostics.append(f"file:{file_index}:frame:{frame_index}:recognition_error:{type(exc).__name__}:{exc}")
                continue
            reward_frames += 1
            for slot, item in enumerate(found):
                if not item.is_known_item():
                    icon = item.image[21:70, 40:89]
                    ok, encoded = cv2.imencode(".png", cv2.cvtColor(icon, cv2.COLOR_RGB2BGR))
                    if not ok:
                        raise ValueError("Candidate encoding failed")
                    raw = encoded.tobytes()
                    id_ = hashlib.sha256(raw).hexdigest()
                    candidates.append({"id": id_, "scene": meta["project"], "image": base64.b64encode(raw).decode(), "sample_sha256": source["sha256"], "occurrence": f"{file_index}:{frame_index}:{slot}", "metadata": {"file_index": file_index, "frame_index": frame_index, "slot": slot, "box": [int(v) for v in item._button.area], "server": meta["server"], "scene": meta["scene"], "amount": int(item.amount)}})
                    diagnostics.append("missing_template:" + id_)
                elif item.amount <= 0:
                    diagnostics.append("invalid_amount:" + item.name)
                else:
                    tag = item.tag or ""
                    rewards[(item.name, tag, int(item.amount))] = {"item": item.name, "tag": tag, "amount": int(item.amount)}
    if meta["scene"] != "research_refresh":
        if not reward_frames or not rewards:
            diagnostics.append("no_complete_reward_frame")
        duration = 0
        if research:
            entry = catalog.get((series, project))
            if entry is None:
                diagnostics.append("unknown_research_project_or_series")
            else:
                value = entry.get("time", entry.get("duration", 0))
                duration = int(value) if isinstance(value, (int, float)) else 0
        merged = {}
        for reward in rewards.values():
            key = reward["item"], reward["tag"]
            if key not in merged:
                merged[key] = dict(reward)
            else:
                merged[key]["amount"] += reward["amount"]
        if not research or (series and project):
            events.append({"event_type": "complete" if research else "drop", "series": int(series), "project": project, "focus_series": 0, "duration_seconds": duration, "items": sorted(merged.values(), key=lambda x: (x["item"], x["tag"]))})
    if len(events) > 1000 or len(candidates) > 1000:
        raise ValueError("Recognition output exceeds safety limit")
    diagnostics = sorted(set(diagnostics))
    return {"events": events, "diagnostics": diagnostics, "candidates": candidates, "used_templates": sorted(used), "status": "needs_review" if diagnostics or not events else "complete"}


if __name__ == "__main__":
    request_path, output_path = sys.argv[1:3]
    try:
        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        result = recognize(request)
    except Exception:
        result = {"events": [], "diagnostics": [traceback.format_exc()[-2000:]], "candidates": [], "used_templates": [], "status": "needs_review"}
    Path(output_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
