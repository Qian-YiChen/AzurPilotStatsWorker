"""Commission page pairing using the pinned client's assets and OCR runtime."""
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata


def normalize_name(value):
    value = unicodedata.normalize("NFKC", value).upper().replace("曰", "日")
    return re.sub(r"[\s\\/_\-'\"“”.,、，。()（）\[\]]", "", value)


@lru_cache(maxsize=1)
def name_catalog():
    entries = json.loads(Path(__file__).with_name("commission_names.json").read_text(encoding="utf-8"))["entries"]
    aliases = {}
    for entry in entries:
        for server, names in entry["aliases"].items():
            for name in names:
                key = server, normalize_name(name)
                # An alias shared by different names cannot establish identity.
                if key in aliases and aliases[key] != entry["name"]:
                    aliases[key] = None
                else:
                    aliases[key] = entry["name"]
    return aliases


def resolve_name(value, server):
    server = "cn" if server.startswith("cn") else server
    name = normalize_name(value)
    variants = {name}
    if name.endswith("1"):
        variants.add(name[:-1] + "I")
    matches = {name_catalog().get((server, variant)) for variant in variants} - {None}
    if len(matches) != 1:
        raise ValueError("unknown_or_ambiguous_commission_name")
    return matches.pop()


class NativeCommissionReader:
    def __init__(self, stats, server):
        from module.azur_stats.assets import COMMISSION_DONE, COMMISSION_NAME, COMMISSION_PERFECT
        from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2, GET_ITEMS_3
        from module.ocr.ocr import Ocr
        from module.ui.assets import COMMISSION_CHECK

        self.stats = stats
        self.server = "cn" if server.startswith("cn") else server
        self.success_markers = (COMMISSION_DONE, COMMISSION_PERFECT)
        self.reward_markers = (GET_ITEMS_1, GET_ITEMS_2, GET_ITEMS_3)
        self.exit_marker = COMMISSION_CHECK
        language = {"cn": "cnocr", "en": "ppocr_v6", "jp": "jp", "tw": "tw"}[self.server]
        self.name_ocr = Ocr(COMMISSION_NAME, lang=language, name="Commission_name")

    def classify(self, image):
        for status, marker in zip(("success", "perfect"), self.success_markers):
            if marker.match_template_color(image, offset=(10, 10)):
                return status
        if any(marker.match_template_color(image, offset=(5, 0)) for marker in self.reward_markers):
            return "reward"
        if self.exit_marker.match_template_color(image, offset=(20, 20)):
            return "exit"
        return "unknown"

    def read_name(self, image):
        return resolve_name(self.name_ocr.ocr(image), self.server)

    def read_items(self, image):
        return self.stats.stats_get_items(image, tag=True)


def recognize_commissions(frame_iterator, reader, used_templates, diagnostics, candidate_builder):
    """Parse one source group in file/frame order, independently of its batch."""
    events, skipped, candidates = [], [], []
    pending = None
    seen = set()

    def skip(location, reason, error=True):
        # Diagnostics determine group status, independently of the bounded log.
        if error:
            diagnostics.append(f"file:{location[0]}:frame:{location[1]}:{reason}")
        if len(skipped) < 1000:
            skipped.append({"file_index": location[0], "frame_index": location[1], "reason": reason})

    def finish_pending():
        nonlocal pending
        current, pending = pending, None
        if current is None:
            return
        if current["reward_count"] != 1:
            skip(current["location"], f"invalid_pairing:reward_count:{current['reward_count']}")
            return
        if current["name"] is None or current["items"] is None:
            return
        signature = current["status"], current["name"], current["reward_entries"]
        if not current["failed"] and signature in seen:
            skip(current["location"], "duplicate_commission", error=False)
            return
        if len(events) >= 1000:
            raise ValueError("Recognition output exceeds safety limit")
        if not current["failed"]:
            seen.add(signature)
        # C groups drop statistics by event.project; source metadata stays drop/commission.
        events.append({"event_type": "drop", "series": 0, "project": current["name"], "focus_series": 0,
                       "duration_seconds": 0, "items": current["items"]})

    for file_index, frame_index, image in frame_iterator:
        location = file_index, frame_index
        if image is None:
            finish_pending()
            skip(location, "unreadable_frame")
            continue
        try:
            kind = reader.classify(image)
        except Exception as exc:
            finish_pending()
            skip(location, "classification_error:" + type(exc).__name__)
            continue
        if kind in ("success", "perfect"):
            finish_pending()
            pending = {"location": location, "status": kind, "name": None, "reward_count": 0,
                       "items": None, "reward_entries": None, "failed": False}
            try:
                pending["name"] = reader.read_name(image)
            except Exception as exc:
                skip(location, "commission_name_error:" + type(exc).__name__)
            continue
        if kind == "exit":
            finish_pending()
            break
        if kind != "reward":
            # Losing a page boundary must not attach a later reward to an old name.
            finish_pending()
            skip(location, "unclassified_frame")
            continue
        if pending is None:
            skip(location, "reward_without_commission")
            continue
        pending["reward_count"] += 1
        # Delay acceptance until the next boundary so a second reward invalidates
        # the whole record instead of leaving a seemingly complete first page.
        if pending["reward_count"] != 1:
            continue
        try:
            found = list(reader.read_items(image))
            if not found:
                raise ValueError("empty_reward")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                "empty_reward"} else type(exc).__name__
            skip(location, "commission_reward_error:" + reason)
            continue
        merged, original_entries = {}, []
        for slot, item in enumerate(found):
            if not item.is_known_item():
                candidate = candidate_builder(item, file_index, frame_index, slot)
                candidates.append(candidate)
                diagnostics.append("missing_template:" + candidate["id"])
                pending["failed"] = True
                continue
            amount = int(item.amount)
            key = item.name, item.tag or ""
            if not 0 < amount <= 1_000_000_000 or merged.get(key, 0) + amount > 1_000_000_000:
                skip(location, "commission_reward_error:invalid_amount")
                pending["failed"] = True
                continue
            original_entries.append((*key, amount))
            merged[key] = merged.get(key, 0) + amount
        # Keep recognized rows for review, including when another slot is unknown.
        # Only a group without diagnostics can contribute to C's statistics.
        pending["reward_entries"] = tuple(sorted(original_entries))
        pending["items"] = [{"item": name, "tag": tag, "amount": amount}
                            for (name, tag), amount in sorted(merged.items())]
    finish_pending()
    if not events:
        diagnostics.append("no_complete_commission")
    failures = sorted(set(diagnostics))
    if len(candidates) > 1000 or len(failures) > 1000:
        raise ValueError("Recognition output exceeds safety limit")
    return {"events": events, "diagnostics": failures,
            "candidates": candidates, "used_templates": sorted(used_templates),
            "status": "needs_review" if failures or not events else "complete",
            "skipped_frames": skipped}
