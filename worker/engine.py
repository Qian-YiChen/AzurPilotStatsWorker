import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .clients import Pan, Statistics

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.stats = Statistics(config)
        self.pan = Pan(config)
        self.work = config.data / "work"
        self.work.mkdir(exist_ok=True)
        # A worker is deliberately single-process. No other process may share
        # this work directory or SQLite DB. Stale originals are not retained.
        for path in self.work.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recognition")

    def recognize(self, source, templates=None):
        with tempfile.TemporaryDirectory(dir=self.work) as temporary:
            directory = Path(temporary)
            paths = []
            for file in source["files"]:
                if shutil.disk_usage(self.work).free < file["size"] + 128 * 1024**2:
                    raise RuntimeError("Insufficient B working disk space")
                path = directory / f"{file['index']}.png"
                self.pan.download(file, path)
                paths.append(str(path))
            request = {"source": source, "files": paths, "alas_root": str(self.config.alas_root), "templates": self.db.setting("templates", []) if templates is None else templates}
            input_path = directory / "input.json"
            output_path = directory / "output.json"
            input_path.write_text(json.dumps(request), encoding="utf-8")
            python = os.getenv("ALAS_PYTHON", sys.executable)
            subprocess.run([python, str(Path(__file__).with_name("recognize.py")), str(input_path), str(output_path)], check=True, timeout=int(os.getenv("RECOGNITION_TIMEOUT", "600")), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if output_path.stat().st_size > 8 * 1024**2:
                raise ValueError("Recognition output is too large")
            return json.loads(output_path.read_text(encoding="utf-8"))

    def process(self, job):
        try:
            source = json.loads(job["source"])
            if job["state"] != "delivery":
                revision = self.stats.revision(source["sha256"]) + 1
                release_id, templates = self.db.release()
                parsed = self.recognize(source, templates)
                result = {"sha256": source["sha256"], "revision": revision, "status": parsed["status"], "parser_version": "azurpilot-adapter-v1", "template_release": release_id, "source": source, "events": parsed["events"], "diagnostics": parsed["diagnostics"], "candidate_ids": sorted({c["id"] for c in parsed["candidates"]}), "used_templates": parsed["used_templates"]}
                self.db.save_result(job, result, parsed["candidates"])
                candidates = parsed["candidates"]
            else:
                result = json.loads(job["result"])
                candidates = json.loads(job["candidates"] or "[]")
            for candidate in candidates:
                self.stats.post("/internal/v1/template-candidates", candidate)
            ack = self.stats.post("/internal/v1/statistics/results", {"results": [result]})["receipts"][0]
            if ack["status"] in ("accepted", "duplicate"):
                self.db.finish(job)
                active = self.db.setting("release_id", 0)
                if active != result["template_release"]:
                    # Publication may race an in-flight first result, before C
                    # knows that source. It must not miss the new template pass.
                    self.db.enqueue_release(source, active)
            elif ack["status"] == "retry":
                raise RuntimeError(ack.get("error", "C temporarily unavailable"))
            else:
                self.db.quarantine(job, json.dumps(ack, ensure_ascii=False))
        except Exception as exc:
            log.exception("Recognition/delivery will retry")
            self.db.retry(job, exc)
        finally:
            if job["task"].startswith("command:"):
                self.db.update_reprocess_command(int(job["task"].split(":")[1]))

    def synchronize(self):
        updates = self.db.sync_updates()
        reply = self.stats.post("/internal/v1/worker/sync", {"worker_id": self.config.worker_id, "release_id": self.db.setting("release_id", 0), "pending": self.db.pending(), "updates": updates})
        release = reply.get("release_id") or 0
        if release != self.db.setting("release_id", 0):
            templates = self.stats.release(release) if release else []
            # Atomic snapshot: no task can observe half a package.
            self.db.set_release(release, templates)
        self.db.accept_commands(reply["commands"], updates)

    def control(self, row):
        cmd = json.loads(row["payload"])
        try:
            if cmd["action"] == "reprocess":
                self.db.enqueue_reprocess(cmd)
                self.db.update_reprocess_command(cmd["id"])
            elif cmd["action"] == "test":
                reports = []
                for source in cmd["payload"]["sources"]:
                    parsed = self.recognize(source, cmd["payload"]["templates"])
                    reports.append({"sha256": source["sha256"], "status": parsed["status"], "events": parsed["events"], "diagnostics": parsed["diagnostics"]})
                self.db.command_result(cmd["id"], "done", {"reports": reports})
            else:
                raise ValueError("Unsupported command")
        except Exception as exc:
            self.db.command_result(cmd["id"], "failed", {"error": str(exc)})

    async def run(self):
        loop = asyncio.get_running_loop()
        async def management():
            while True:
                try:
                    sync = loop.run_in_executor(None, self.synchronize)
                    try:
                        await asyncio.shield(sync)
                    except asyncio.CancelledError:
                        await sync  # Finish HTTP activity before closing clients.
                        raise
                except Exception:
                    log.exception("C control sync failed")
                await asyncio.sleep(30)
        management_task = asyncio.create_task(management())
        try:
            while True:
                command = self.db.command()
                if command:
                    await loop.run_in_executor(self.executor, self.control, command)
                    continue
                job = self.db.claim()
                if job:
                    await loop.run_in_executor(self.executor, self.process, job)
                else:
                    await asyncio.sleep(0.5)
        finally:
            management_task.cancel()
            try:
                await management_task
            except asyncio.CancelledError:
                pass
            self.executor.shutdown(wait=True)
            self.stats.client.close()
            self.pan.client.close()
