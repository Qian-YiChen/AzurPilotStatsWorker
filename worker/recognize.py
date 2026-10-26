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


def research_focus(image, series_list):
    # Same visual check and >=3 majority rule as AzurStats ResearchList.
    from module.azur_stats.image.base import ImageBase
    from module.azur_stats.assets import FOCUS_SERIES
    if not ImageBase().image_color_count(image, FOCUS_SERIES, (41, 50, 57), threshold=235, count=100):
        return 0
    return next((int(s) for s in series_list if s and series_list.count(s) >= 3), 0)


def frames(paths, diagnostics):
    import numpy as np
    from PIL import Image
    for file_index, path in enumerate(paths):
        with Image.open(path) as png:
            if png.format != "PNG" or png.width != 1280 or png.height % 720 or not 1 <= png.height // 720 <= 10:
                diagnostics.append(f"file:{file_index}:unsupported_resolution")
                continue
            for index in range(png.height // 720):
                yield file_index, index, np.asarray(png.crop((0, index * 720, 1280, (index + 1) * 720)).convert("RGB"))


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
    meta = dict(source["metadata"])
    diagnostics = []
    overrides = {}
    if meta["scene"] == "legacy_auto":
        from module.research.assets import QUEUE_CHECK
        from module.ui.assets import RESEARCH_CHECK
        from module.azur_stats.scene.operation_siren import SceneOperationSiren
        detector = SceneOperationSiren()
        for _, _, frame in frames(request["files"], diagnostics):
            if QUEUE_CHECK.match(frame, offset=(10, 10)):
                overrides = {"project": "research", "scene": "research_complete"}
                break
            if RESEARCH_CHECK.match(frame, offset=(10, 10)):
                overrides = {"project": "research", "scene": "research_refresh"}
            elif detector.is_opsi_zone(frame) and not overrides:
                overrides = {"project": "drop", "scene": "opsi_meowfficer_farming"}
        if not overrides:
            return {"events": [], "diagnostics": ["legacy_scene_requires_classification"], "candidates": [], "used_templates": [], "status": "needs_review"}
        meta.update(overrides)
    research = meta["project"] == "research"
    opsi = meta["scene"].startswith("opsi_")
    parser = ResearchDropParser() if research else None
    zone = {"zone": "", "zone_type": "UNKNOWN", "zone_id": 0, "hazard_level": 0}
    cleared = (-1, -1)
    if parser:
        stats = parser.stats
        grids = [stats._target_grid()]
    elif opsi:
        from module.azur_stats.scene.operation_siren import SceneOperationSiren
        stats = SceneOperationSiren()
        grids = [stats.item_grid, stats.auto_search_item_group]
        for file_index, frame_index, frame in frames(request["files"], diagnostics):
            if stats.is_opsi_zone(frame):
                try:
                    parsed_zone = stats.parse_opsi_zone(frame)
                    zone = {key: getattr(parsed_zone, key) for key in zone}
                    cleared = (file_index, frame_index)
                except Exception:
                    diagnostics.append("unrecognized_opsi_zone")
                break
    else:
        stats = GetItemsStatistics()
        stats.grid = ItemGrid(None, {})
        if meta["scene"].startswith("commission"):
            stats.load_template_folder("./assets/stats/commission_items")
        else:
            stats.load_template_folder("./assets/stats/battle_items")
        grids = [stats._target_grid()]
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
        for grid in grids:
            grid.templates[key] = image
            grid.colors[key] = cv2.mean(image)[:3]
            grid.templates_hit[key] = 0
    used = set()
    def track(match):
        def tracked_match(image, *args, **kwargs):
            key = match(image, *args, **kwargs)
            if key in overlay:
                used.add(overlay[key])
            return key
        return tracked_match
    for grid in grids:
        grid.match_template = track(grid.match_template)
    candidates = []
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

    for file_index, frame_index, frame in frames(request["files"], diagnostics):
        if research and meta["scene"] == "research_refresh":
            from module.research.project import get_research_name
            from module.research.series import get_research_series_3
            names = get_research_name(frame)
            series_list = get_research_series_3(frame)
            if len(names) != 5 or len(series_list) != 5 or any((int(s), n) not in catalog for s, n in zip(series_list, names)):
                diagnostics.append("research_refresh:incomplete_project_list")
            else:
                focus = research_focus(frame, series_list)
                for s, n in zip(series_list, names):
                    events.append({"event_type": "refresh", "series": int(s), "project": n, "focus_series": focus, "duration_seconds": 0, "items": []})
            continue
        if research and QUEUE_CHECK.match(frame, offset=(10, 10)):
            project = parser._read_project(frame)
            series = parser._read_series(frame)
            continue
        try:
            forced_tag = None
            if opsi:
                if (file_index, frame_index) == cleared:
                    continue
                if stats.is_get_items(frame):
                    found = list(stats.parse_get_items(frame))
                    forced_tag = "log" if (file_index, frame_index) > cleared else None
                elif stats.is_opsi_reward(frame):
                    found = list(stats.parse_auto_search_reward(frame))
                    forced_tag = "scan" if (file_index, frame_index) > cleared else None
                else:
                    continue
            else:
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
                tag = forced_tag or item.tag or ""
                key = (file_index, frame_index, slot) if opsi else (item.name, tag, int(item.amount))
                rewards[key] = {"item": item.name, "tag": tag, "amount": int(item.amount)}
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
            event = {"event_type": "complete" if research else "drop", "series": int(series), "project": project, "focus_series": 0, "duration_seconds": duration, "items": sorted(merged.values(), key=lambda x: (x["item"], x["tag"]))}
            if opsi:
                event.update(zone)
            events.append(event)
    if len(events) > 1000 or len(candidates) > 1000:
        raise ValueError("Recognition output exceeds safety limit")
    diagnostics = sorted(set(diagnostics))
    return {"events": events, "metadata_overrides": overrides, "diagnostics": diagnostics, "candidates": candidates, "used_templates": sorted(used), "status": "needs_review" if diagnostics or not events else "complete"}


if __name__ == "__main__":
    request_path, output_path = sys.argv[1:3]
    try:
        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        result = recognize(request)
    except Exception:
        result = {"events": [], "diagnostics": [traceback.format_exc()[-2000:]], "candidates": [], "used_templates": [], "status": "needs_review"}
    Path(output_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
