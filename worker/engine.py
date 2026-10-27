import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import tempfile
import time

from .clients import Pan, Statistics, WebDAV
from .pool import RecognizerPool, automatic_workers

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.stats = Statistics(config)
        self.pan = WebDAV(config) if config.webdav_url else Pan(config)
        self.work = config.data / "work"
        self.work.mkdir(exist_ok=True)
        # A worker is deliberately single-process. No other process may share
        # this work directory or SQLite DB. Stale originals are not retained.
        for path in self.work.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        self.workers = config.recognition_workers or automatic_workers()
        self.job_slots = self.workers + (config.prefetch_jobs or max(1, self.workers // 2))
        self.executor = ThreadPoolExecutor(max_workers=self.job_slots, thread_name_prefix="recognition")
        self.recognizers = RecognizerPool(self.workers, config.ocr_threads)
        self.disk_lock = threading.Lock()
        self.disk_reserved = 0
        log.info('Recognition lanes=%s job_slots=%s threads_per_process=%s mode=resident',
                 self.workers, self.job_slots, config.ocr_threads)

    def recognize(self, source, templates=None):
        with tempfile.TemporaryDirectory(dir=self.work) as temporary:
            directory = Path(temporary)
            paths = []
            for file in source["files"]:
                with self.disk_lock:
                    if shutil.disk_usage(self.work).free - self.disk_reserved < file['size'] + 128 * 1024**2:
                        raise RuntimeError('Insufficient B working disk space')
                    self.disk_reserved += file['size']
                path = directory / f"{file['index']}.png"
                started = time.monotonic()
                try:
                    self.pan.download(file, path)
                finally:
                    with self.disk_lock:
                        self.disk_reserved -= file['size']
                log.info("Original downloaded sha=%s index=%s bytes=%s seconds=%.3f transport=%s",
                         source["sha256"], file["index"], file["size"], time.monotonic() - started,
                         "webdav" if self.config.webdav_url else "openapi")
                paths.append(str(path))
            request = {"source": source, "files": paths, "alas_root": str(self.config.alas_root), "templates": self.db.setting("templates", []) if templates is None else templates}
            input_path = directory / "input.json"
            output_path = directory / "output.json"
            input_path.write_text(json.dumps(request), encoding="utf-8")
            started = time.monotonic()
            self.recognizers.recognize(input_path, output_path, source['metadata']['server'],
                                       timeout=int(os.getenv('RECOGNITION_TIMEOUT', '600')))
            if output_path.stat().st_size > 8 * 1024**2:
                raise ValueError("Recognition output is too large")
            parsed = json.loads(output_path.read_text(encoding="utf-8"))
            log.info("OCR completed sha=%s status=%s seconds=%.3f diagnostics=%s", source["sha256"],
                     parsed["status"], time.monotonic() - started, len(parsed["diagnostics"]))
            return parsed

    def process(self, job):
        try:
            source = json.loads(job["source"])
            if job["state"] != "delivery":
                revision = self.stats.revision(source["sha256"]) + 1
                release_id, templates = self.db.release()
                parsed = self.recognize(source, templates)
                if ({"no_complete_reward_frame", "unknown_research_project_or_series"}
                        <= set(parsed["diagnostics"]) and self.db.is_redundant_research_list(source)):
                    parsed = {"status": "invalid", "events": [], "diagnostics": ["duplicate_research_list_capture"],
                              "candidates": [], "used_templates": []}
                overrides = parsed.get("metadata_overrides", {})
                if set(overrides) - {"project", "scene"} or any(not isinstance(v, str) or len(v) > 128 for v in overrides.values()):
                    raise ValueError("Invalid inferred metadata")
                source["metadata"].update(overrides)
                result = {"sha256": source["sha256"], "revision": revision, "status": parsed["status"], "parser_version": "azurpilot-adapter-v2", "template_release": release_id, "source": source, "events": parsed["events"], "diagnostics": parsed["diagnostics"], "candidate_ids": sorted({c["id"] for c in parsed["candidates"]}), "used_templates": parsed["used_templates"]}
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
                log.info("Result delivered sha=%s revision=%s status=%s", source["sha256"],
                         result["revision"], result["status"])
                active = self.db.setting("release_id", 0)
                if active != result["template_release"]:
                    # Publication may race an in-flight first result, before C
                    # knows that source. It must not miss the new template pass.
                    self.db.enqueue_release(source, active)
            elif ack["status"] == "retry":
                raise RuntimeError(ack.get("error", "C temporarily unavailable"))
            else:
                self.db.quarantine(job, json.dumps(ack, ensure_ascii=False))
                log.error("Result rejected sha=%s receipt=%s error=%s", source["sha256"], ack["status"], ack.get("error", ""))
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
        async def execute(function, *args):
            future = loop.run_in_executor(self.executor, function, *args)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                await future  # Drain leases/results before closing SQLite or HTTP.
                raise

        async def consume():
            while True:
                job = self.db.claim()
                if job:
                    await execute(self.process, job)
                else:
                    await asyncio.sleep(0.1)

        async def controls():
            while True:
                command = self.db.command()
                if command:
                    # The default executor keeps template commands independent
                    # of bounded ingestion/prefetch lanes.
                    future = loop.run_in_executor(None, self.control, command)
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        await future
                        raise
                else:
                    await asyncio.sleep(0.1)

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
        tasks = [asyncio.create_task(management()), asyncio.create_task(controls())]
        tasks.extend(asyncio.create_task(consume()) for _ in range(self.job_slots))
        try:
            await asyncio.shield(asyncio.gather(*tasks))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.executor.shutdown(wait=True)
            self.recognizers.close()
            self.stats.client.close()
            self.pan.client.close()
