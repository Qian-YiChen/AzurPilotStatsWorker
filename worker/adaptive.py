"""Feedback-controlled stage concurrency within CPU, memory and disk budgets."""
import math
from pathlib import Path
import threading
import time

import psutil

from .pool import hardware

MIB = 1024**2


class Resources:
    def __init__(self):
        self.cpus, self.memory = hardware()
        self.previous = None

    def sample(self):
        now = time.monotonic()
        try:
            values = dict(line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())
            used = int(values['usage_usec']) / 1e6
        except (OSError, KeyError, ValueError):
            process = psutil.Process()
            used = 0
            for child in [process, *process.children()]:
                try:
                    used += sum(child.cpu_times()[:2])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        cpu = 0.0
        if self.previous:
            elapsed = max(.01, now - self.previous[0])
            cpu = max(0, (used - self.previous[1]) / elapsed / self.cpus)
        self.previous = (now, used)
        host = psutil.virtual_memory()
        memory = 1 - host.available / host.total
        try:
            current = int(Path('/sys/fs/cgroup/memory.current').read_text())
            stats = dict(line.split() for line in Path('/sys/fs/cgroup/memory.stat').read_text().splitlines())
            # Reclaimable file cache must not look like resident OCR models.
            current -= int(stats.get('inactive_file', 0))
            memory = max(memory, current / self.memory)
        except (OSError, ValueError):
            pass
        return {'cpu': round(cpu, 3), 'memory': round(memory, 3),
                'host_cpu': psutil.cpu_percent() / 100}


class Adaptive:
    def __init__(self, config):
        self.resources = Resources()
        cpus, memory = self.resources.cpus, self.resources.memory
        # One core per recognizer; reserve space for HTTP, arrays and the OS.
        resident = max(1, min(cpus, (memory - max(512 * MIB, memory // 5)) // (384 * MIB)))
        self.maximum = {
            'download': config.download_workers or min(256, max(16, cpus * 8)),
            'verify': config.verify_workers or min(8, max(1, cpus // 2)),
            'recognize': min(config.ocr_processes or resident, resident,
                             config.ocr_slots or resident),
            'deliver': config.deliver_workers or min(32, max(4, cpus * 2)),
        }
        self.enabled = config.auto_tune
        self.limits = dict(self.maximum)
        if self.enabled:
            for stage, initial in (('download', 4), ('verify', 1),
                                   ('recognize', max(1, cpus // 2)), ('deliver', 2)):
                self.limits[stage] = min(initial, self.maximum[stage])
        self.ahead = config.download_ahead or max(4, self.maximum['recognize'] * 3)
        self.lock = threading.RLock()
        self.completed = dict.fromkeys(self.maximum, 0)
        self.files = dict.fromkeys(self.maximum, 0)
        self.errors = dict.fromkeys(self.maximum, 0)
        self.seconds = dict.fromkeys(self.maximum, 0.0)
        self.cooldown = 0.0
        self.throttles = 0
        self.started = time.monotonic()
        self.pressure = {'cpu': 0, 'memory': 0, 'host_cpu': 0}

    def limit(self, stage):
        with self.lock:
            return self.limits[stage]

    def observe(self, stage, elapsed, files, failed=False):
        with self.lock:
            self.completed[stage] += int(not failed)
            self.files[stage] += files if not failed else 0
            self.errors[stage] += int(failed)
            self.seconds[stage] += elapsed

    def throttled(self, seconds=10):
        with self.lock:
            now = time.monotonic()
            if now >= self.cooldown:
                self.throttles += 1
                self.limits['download'] = max(1, self.limits['download'] // 2)
            self.cooldown = max(self.cooldown, now + seconds)

    def download_ready(self):
        with self.lock:
            return time.monotonic() >= self.cooldown

    def adjust(self, counts, pressure=None):
        pressure = self.resources.sample() if pressure is None else pressure
        with self.lock:
            self.pressure = pressure
            if not self.enabled:
                return
            def change(stage, delta):
                self.limits[stage] = max(1, min(self.maximum[stage], self.limits[stage] + delta))
            other_cpu = max(0, pressure['host_cpu'] * (psutil.cpu_count() or self.resources.cpus)
                            - pressure['cpu'] * self.resources.cpus)
            available = max(1, math.ceil(self.resources.cpus - other_cpu))
            if pressure['memory'] >= .85:
                change('recognize', -1)
                change('download', -max(1, self.limits['download'] // 4))
            elif pressure['host_cpu'] >= .97 and self.limits['recognize'] > available:
                change('recognize', -1)
            elif (counts.get('verified', 0) >= self.limits['recognize']
                  and pressure['cpu'] < .93 and pressure['host_cpu'] < .97):
                change('recognize', 1)
            buffered = counts.get('downloaded', 0) + counts.get('verified', 0)
            target = min(self.ahead, max(2, self.limits['recognize'] * 2))
            if buffered >= target:
                change('download', -max(1, self.limits['download'] // 4))
            elif (counts.get('pending', 0) and buffered < target // 2
                  and pressure['memory'] < .8 and self.download_ready()
                  and (pressure['cpu'] < .93 or (not buffered and self.limits['download'] < self.limits['recognize']))):
                change('download', max(1, math.ceil(self.limits['download'] / 4)))
            if counts.get('delivery', 0) > self.limits['deliver']:
                change('deliver', 1)
            if counts.get('downloaded', 0) > self.limits['verify'] and pressure['cpu'] < .9:
                change('verify', 1)

    def snapshot(self):
        with self.lock:
            elapsed = max(1, time.monotonic() - self.started)
            return {'automatic': self.enabled, 'cpu_capacity': self.resources.cpus,
                    'memory_budget': self.resources.memory, 'pressure': dict(self.pressure),
                    'limits': dict(self.limits), 'maximum': dict(self.maximum),
                    'download_backoff_seconds': round(max(0, self.cooldown - time.monotonic()), 1),
                    'download_throttles': self.throttles,
                    'stages': {stage: {'completed': self.completed[stage], 'errors': self.errors[stage],
                                      'images_per_second': round(self.files[stage] / elapsed, 3),
                                      'mean_seconds': round(self.seconds[stage] / max(1, self.completed[stage]), 3)}
                               for stage in self.maximum}}
