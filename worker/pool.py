"""Bounded resident recognizer processes; only private job paths cross stdin.

Each process handles one request at a time and is replaced on timeout, crash,
region change or periodic retirement. It never owns the scheduler's SQLite DB.
OpenCV and OCR stages own separate pools, so one stage can never starve the
other by occupying its processes.
"""
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


def hardware():
    """CPU count and memory budget for the worker pools."""
    cpus = getattr(os, 'process_cpu_count', os.cpu_count)() or 1
    try:
        quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()
        if quota != 'max':
            cpus = min(cpus, max(1, math.ceil(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    memory = 2 * 1024**3
    try:
        limit = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        if limit != 'max':
            memory = int(limit)
        else:
            raise ValueError('unlimited cgroup')
    except (OSError, ValueError):
        try:
            memory = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
        except (AttributeError, OSError, ValueError):
            pass
    return max(1, cpus), memory


def automatic_workers():
    cpus, memory = hardware()
    # Leave the API, SQLite, transient arrays and OS headroom outside each lane.
    by_memory = max(1, (memory - 256 * 1024**2) // (384 * 1024**2))
    return max(1, min(cpus, by_memory, 32))


def stage_processes(requested):
    """Resident process count for one stage, bounded by the real hardware.

    Slots (in-flight groups) can exceed this: the extra entries queue in the
    stage instead of each pinning hundreds of MiB of OCR runtime.
    """
    if requested:
        return max(1, min(requested, automatic_workers() * 2))
    cpus, memory = hardware()
    by_memory = max(1, (memory - 512 * 1024**2) // (384 * 1024**2))
    return max(1, min(cpus, by_memory // 2, 32))


class RecognizerPool:
    def __init__(self, size, threads=1, stage='recognize'):
        self.size, self.threads, self.stage = size, threads, stage
        self.slots = queue.LifoQueue()
        for _ in range(size):
            self.slots.put(None)
        self.processes = set()
        self.lock = threading.Lock()

    def _stop(self, process):
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        with self.lock:
            self.processes.discard(process)

    def run(self, input_path, output_path, server, timeout=600):
        region = 'cn' if server.startswith('cn') else server
        process = self.slots.get()
        try:
            if process is None or process.poll() is not None or process.region != region:
                self._stop(process)
                env = dict(os.environ)
                for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                            'NUMEXPR_NUM_THREADS', 'AZURPILOT_OCR_THREADS'):
                    env[key] = str(self.threads)
                env['RECOGNIZER_STAGE'] = self.stage
                process = subprocess.Popen(
                    [os.getenv('ALAS_PYTHON', sys.executable), str(Path(__file__).with_name('recognize.py')), '--serve'],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    text=True, encoding='utf-8', env=env,
                )
                process.region = region
                with self.lock:
                    self.processes.add(process)
            process.stdin.write(json.dumps([str(input_path), str(output_path)]) + '\n')
            process.stdin.flush()
            deadline = time.monotonic() + timeout
            while not output_path.exists():
                if process.poll() is not None:
                    if output_path.exists():
                        break
                    raise RuntimeError('Resident recognizer exited before producing a result')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Resident recognition timed out')
                time.sleep(0.01)
        except BaseException:
            self._stop(process)
            process = None
            raise
        finally:
            self.slots.put(process)

    # Kept for compatibility with existing callers and tests.
    def recognize(self, input_path, output_path, server, timeout=600):
        return self.run(input_path, output_path, server, timeout)

    def close(self):
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            self._stop(process)
