import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from .clients import Pan, Statistics, WebDAV
from .pool import RecognizerPool, stage_processes

log = logging.getLogger(__name__)

MAX_STAGE_ATTEMPTS = 5


def template_fields(template):
    """Comparable identity of one published template, without the image bytes."""
    image = template.get('image') or ''
    if isinstance(image, str) and len(image) > 200:
        image = hashlib.sha256(image.encode()).hexdigest()
    return (template.get('item_id'), template.get('scene'), template.get('enabled'), image)


def template_snapshot(templates):
    snapshot = []
    for template in templates:
        item = {key: template.get(key) for key in ('item_id', 'scene', 'enabled')}
        item['id'] = template['id']
        item['image'] = template_fields(template)[3]
        snapshot.append(item)
    return snapshot


def needs_template_retry(result, previous, current):
    if result.get('candidate_ids') or any(d == 'missing_template' or d.startswith('missing_template:')
                                          for d in result.get('diagnostics', [])):
        return True
    used = result.get('used_templates', [])
    if not used:
        return False
    if previous is None:
        # A saved delivery from an older process has no in-memory snapshot.
        return True
    old = {t['id']: template_fields(t) for t in previous}
    new = {t['id']: template_fields(t) for t in current}
    return any(old.get(id_) != new.get(id_) for id_ in used)


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
        self.download_workers = config.download_workers
        self.verify_workers = config.verify_workers
        self.recognize_slots = config.ocr_slots
        self.deliver_workers = config.deliver_workers
        self.download_ahead = config.download_ahead
        self.recognize_processes = stage_processes(config.ocr_processes)
        self.recognizers = RecognizerPool(self.recognize_processes, config.ocr_threads, stage='recognize')
        self.workers = self.recognize_processes
        self.executor = ThreadPoolExecutor(
            max_workers=max(self.download_workers, self.verify_workers, self.deliver_workers) + 4,
            thread_name_prefix="stage")
        self.disk_lock = threading.Lock()
        log.info('Pipeline staged: download=%s verify=%s recognize_slots=%s recognize_processes=%s '
                 'deliver=%s download_ahead=%s pipeline=%s',
                 self.download_workers, self.verify_workers, self.recognize_slots,
                 self.recognize_processes, self.deliver_workers, self.download_ahead, config.pipeline)

    # ---- shared helpers -------------------------------------------------
    def source(self, job):
        return json.loads(job["source"])

    def workdir(self, job):
        return self.work / str(job["id"])

    def files(self, job):
        source = self.source(job)
        directory = self.workdir(job)
        return [directory / f"{file['index']}.png" for file in source["files"]]

    def cleanup(self, job):
        root = getattr(self, 'work', None)
        if root is None:
            return
        shutil.rmtree(root / str(job['id']), ignore_errors=True)

    def fail(self, job, state, stage, error):
        attempts = int(job.get('attempts') or 0) + 1
        if attempts >= MAX_STAGE_ATTEMPTS:
            self.db.quarantine(job, f'{stage}: {error}')
            self.cleanup(job)
            log.error('stage=%s quarantined job=%s attempts=%s error=%s',
                      stage, job['id'], attempts, str(error)[:300])
        else:
            self.db.retry_stage(job, state, f'{stage}: {error}')
            log.warning('stage=%s retry job=%s attempts=%s error=%s',
                        stage, job['id'], attempts, str(error)[:300])

    def run_recognizer(self, request, job, stage):
        directory = self.workdir(job)
        input_path = directory / f'{stage}.input.json'
        output_path = directory / f'{stage}.output.json'
        input_path.write_text(json.dumps(request), encoding='utf-8')
        started = time.monotonic()
        self.recognizers.recognize(input_path, output_path, self.source(job)['metadata']['server'],
                                   timeout=int(os.getenv('RECOGNITION_TIMEOUT', '600')))
        if output_path.stat().st_size > 8 * 1024**2:
            raise ValueError("Recognition output is too large")
        parsed = json.loads(output_path.read_text(encoding="utf-8"))
        input_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        return parsed, time.monotonic() - started

    # ---- stages ---------------------------------------------------------
    def stage_download(self, job):
        source = self.source(job)
        directory = self.workdir(job)
        directory.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            for file in source["files"]:
                path = directory / f"{file['index']}.png"
                one = time.monotonic()
                self.pan.download(file, path)
                log.info('stage=download sha=%s job=%s index=%s bytes=%s seconds=%.3f transport=%s',
                         source["sha256"], job['id'], file["index"], file["size"], time.monotonic() - one,
                         "webdav" if self.config.webdav_url else "openapi")
            self.db.timed(job, 'download_ms', (time.monotonic() - started) * 1000)
            self.db.advance(job, 'downloaded')
            log.info('stage=download complete sha=%s job=%s files=%s seconds=%.3f',
                     source["sha256"], job['id'], len(source["files"]), time.monotonic() - started)
        except Exception as error:
            self.fail(job, 'pending', 'download', error)

    def stage_verify(self, job):
        source = self.source(job)
        directory = self.workdir(job)
        started = time.monotonic()
        try:
            total = 0
            for file in source["files"]:
                path = directory / f"{file['index']}.png"
                # hashlib.file_digest hashes in C over the file object: one call,
                # no Python chunk loop and no GIL-only work while OCR saturates
                # the CPU. The old loop took over a second under load.
                with path.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                size = path.stat().st_size
                if size != file["size"] or digest != file["sha256"]:
                    raise ValueError(f"index {file['index']} hash/size mismatch")
                total += size
            self.db.advance(job, 'verified')
            log.info('stage=verify sha=%s job=%s files=%s bytes=%s seconds=%.3f',
                     source["sha256"], job['id'], len(source["files"]), total, time.monotonic() - started)
        except Exception as error:
            self.fail(job, 'pending', 'verify', error)

    def stage_recognize(self, job):
        source = self.source(job)
        release_id, templates = self.db.release()
        request = {"source": source, "files": [str(path) for path in self.files(job)],
                   "alas_root": str(self.config.alas_root), "templates": templates}
        try:
            parsed, elapsed = self.run_recognizer(request, job, 'recognize')
            if ({"no_complete_reward_frame", "unknown_research_project_or_series"}
                    <= set(parsed["diagnostics"]) and self.db.is_redundant_research_list(source)):
                parsed = {"status": "invalid", "events": [], "diagnostics": ["duplicate_research_list_capture"],
                          "candidates": [], "used_templates": []}
            overrides = parsed.get("metadata_overrides", {})
            if set(overrides) - {"project", "scene"} or any(not isinstance(v, str) or len(v) > 128 for v in overrides.values()):
                raise ValueError("Invalid inferred metadata")
            source["metadata"].update(overrides)
            revision = self.stats.revision(source["sha256"]) + 1
            result = {"sha256": source["sha256"], "revision": revision, "status": parsed["status"],
                      "parser_version": "azurpilot-adapter-v2",
                      "template_release": release_id, "source": source,
                      "events": parsed["events"], "diagnostics": parsed["diagnostics"],
                      "candidate_ids": sorted({c["id"] for c in parsed["candidates"]}),
                      "used_templates": parsed["used_templates"]}
            self.db.timed(job, 'ocr_ms', elapsed * 1000)
            self.db.save_result(job, result, parsed["candidates"], template_snapshot(templates))
            # Hand the lease to the delivery stage; it is a separate worker.
            self.db.advance(job, 'delivery')
            log.info('stage=recognize sha=%s job=%s status=%s events=%s candidates=%s revision=%s seconds=%.3f',
                     source["sha256"], job['id'], parsed["status"], len(parsed["events"]),
                     len(parsed["candidates"]), revision, elapsed)
        except Exception as error:
            self.fail(job, 'verified', 'recognize', error)

    def deliver(self, job, source, result, candidates, snapshot):
        for candidate in candidates:
            self.stats.post("/internal/v1/template-candidates", candidate)
        ack = self.stats.post("/internal/v1/statistics/results", {"results": [result]})["receipts"][0]
        if ack["status"] in ("accepted", "duplicate"):
            self.db.finish(job)
            self.cleanup(job)
            log.info('stage=deliver sha=%s job=%s revision=%s status=%s receipt=%s',
                     source["sha256"], job['id'], result["revision"], result["status"], ack["status"])
            active, current_templates = self.db.release()
            if active != result["template_release"] and needs_template_retry(result, snapshot, current_templates):
                # Publication may race an in-flight first result, before C
                # knows that source. It must not miss the new template pass.
                self.db.enqueue_release(source, active)
        elif ack["status"] == "retry":
            raise RuntimeError(ack.get("error", "C temporarily unavailable"))
        else:
            self.db.quarantine(job, json.dumps(ack, ensure_ascii=False))
            log.error('stage=deliver rejected sha=%s receipt=%s error=%s', source["sha256"], ack["status"], ack.get("error", ""))

    def stage_deliver(self, job):
        source = self.source(job)
        snapshot = json.loads(job["snapshot"]) if job.get("snapshot") else None
        try:
            result = json.loads(job["result"])
            candidates = json.loads(job["candidates"] or "[]")
        except (TypeError, ValueError) as error:
            self.fail(job, 'delivery', 'deliver', error)
            return
        try:
            self.deliver(job, source, result, candidates, snapshot)
        except Exception as error:
            self.fail(job, 'delivery', 'deliver', error)
        finally:
            if job["task"].startswith("command:"):
                self.db.update_reprocess_command(int(job["task"].split(":")[1]))

    # ---- compatibility: single-shot recognition for template tests ------
    def recognize(self, source, templates=None):
        with tempfile.TemporaryDirectory(dir=self.work) as temporary:
            directory = Path(temporary)
            paths = []
            for file in source["files"]:
                path = directory / f"{file['index']}.png"
                self.pan.download(file, path)
                paths.append(str(path))
            request = {"source": source, "files": paths, "alas_root": str(self.config.alas_root),
                       "templates": self.db.setting("templates", []) if templates is None else templates}
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
        """Single-lane path used by PIPELINE=inline and template trials."""
        try:
            source = self.source(job)
            snapshot = json.loads(job["snapshot"]) if job.get("snapshot") else None
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
                result = {"sha256": source["sha256"], "revision": revision, "status": parsed["status"],
                          "parser_version": "azurpilot-adapter-v2", "template_release": release_id,
                          "source": source, "events": parsed["events"], "diagnostics": parsed["diagnostics"],
                          "candidate_ids": sorted({c["id"] for c in parsed["candidates"]}),
                          "used_templates": parsed["used_templates"]}
                snapshot = template_snapshot(templates)
                self.db.save_result(job, result, parsed["candidates"], snapshot)
                candidates = parsed["candidates"]
            else:
                result = json.loads(job["result"])
                candidates = json.loads(job["candidates"] or "[]")
            self.deliver(job, source, result, candidates, snapshot)
        except Exception as exc:
            log.exception("Recognition/delivery will retry")
            self.db.retry(job, exc)
        finally:
            if job["task"].startswith("command:"):
                self.db.update_reprocess_command(int(job["task"].split(":")[1]))

    def synchronize(self):
        updates = self.db.sync_updates()
        reply = self.stats.post("/internal/v1/worker/sync", {"worker_id": self.config.worker_id,
                                                             "release_id": self.db.setting("release_id", 0),
                                                             "pending": self.db.pending(), "updates": updates})
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
                    reports.append({"sha256": source["sha256"], "status": parsed["status"],
                                    "events": parsed["events"], "diagnostics": parsed["diagnostics"]})
                self.db.command_result(cmd["id"], "done", {"reports": reports})
            else:
                raise ValueError("Unsupported command")
        except Exception as exc:
            self.db.command_result(cmd["id"], "failed", {"error": str(exc)})

    # ---- scheduler ------------------------------------------------------
    def stage(self, loop, name, state, slots, handler, budget=None):
        queue = asyncio.Queue(maxsize=slots)

        async def execute(function, *args):
            future = loop.run_in_executor(self.executor, function, *args)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                await future  # Drain leases/results before closing SQLite or HTTP.
                raise

        async def claim():
            while True:
                if budget is not None and not budget():
                    await asyncio.sleep(0.5)
                    continue
                try:
                    job = await loop.run_in_executor(None, self.db.claim_stage, state)
                except Exception:
                    log.exception('stage=%s claim failed', name)
                    await asyncio.sleep(1)
                    continue
                if job is None:
                    await asyncio.sleep(0.25)
                    continue
                await queue.put(job)

        async def consume():
            while True:
                job = await queue.get()
                try:
                    await execute(handler, job)
                except Exception:
                    log.exception('stage=%s worker failed unexpectedly', name)
                    self.fail(job, state, name, 'unexpected worker failure')
                finally:
                    queue.task_done()

        tasks = [asyncio.create_task(claim())]
        tasks.extend(asyncio.create_task(consume()) for _ in range(slots))
        return tasks

    async def run(self):
        loop = asyncio.get_running_loop()
        if self.config.pipeline == "inline":
            return await self.run_inline(loop)

        async def control_worker():
            while True:
                command = await loop.run_in_executor(None, self.db.command)
                if command:
                    future = loop.run_in_executor(None, self.control, command)
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        await future
                        raise
                else:
                    await asyncio.sleep(0.25)

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

        def ahead():
            return self.db.count_states(('downloaded', 'verified')) < self.download_ahead

        async def reporter():
            while True:
                counts = self.db.stage_counts()
                log.info('stage=depth pending=%s downloaded=%s verified=%s delivery=%s done=%s failed=%s',
                         counts.get('pending', 0), counts.get('downloaded', 0), counts.get('verified', 0),
                         counts.get('delivery', 0), counts.get('done', 0), counts.get('failed', 0))
                await asyncio.sleep(30)

        tasks = [asyncio.create_task(management()), asyncio.create_task(control_worker()),
                 asyncio.create_task(reporter())]
        tasks += self.stage(loop, 'download', 'pending', self.download_workers, self.stage_download, budget=ahead)
        tasks += self.stage(loop, 'verify', 'downloaded', self.verify_workers, self.stage_verify)
        tasks += self.stage(loop, 'recognize', 'verified', self.recognize_slots, self.stage_recognize)
        tasks += self.stage(loop, 'deliver', 'delivery', self.deliver_workers, self.stage_deliver)
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

    async def run_inline(self, loop):
        """Previous one-lane pipeline, kept as a rollback switch (PIPELINE=inline)."""
        async def execute(function, *args):
            future = loop.run_in_executor(self.executor, function, *args)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                await future
                raise

        async def consume():
            while True:
                job = await loop.run_in_executor(None, self.db.claim)
                if job:
                    await execute(self.process, job)
                else:
                    await asyncio.sleep(0.2)

        async def management():
            while True:
                try:
                    sync = loop.run_in_executor(None, self.synchronize)
                    try:
                        await asyncio.shield(sync)
                    except asyncio.CancelledError:
                        await sync
                        raise
                except Exception:
                    log.exception("C control sync failed")
                await asyncio.sleep(30)

        tasks = [asyncio.create_task(management())]
        tasks.extend(asyncio.create_task(consume()) for _ in range(max(4, self.deliver_workers)))
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
