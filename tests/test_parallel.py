import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from worker.db import Database
from worker.engine import Engine
from worker.pool import RecognizerPool
from test_worker import batch, config, source


def test_parallel_claims_keep_delivery_and_same_source_exclusive(tmp_path):
    db = Database(tmp_path / 'state.sqlite')
    db.accept(batch(8))
    db.enqueue_reprocess({'id': 42, 'payload': {'sources': [source()]}})
    with ThreadPoolExecutor(12) as executor:
        claimed = [r for r in executor.map(lambda _: db.claim(), range(12)) if r]
    assert len(claimed) == 8 and len({r['sha'] for r in claimed}) == 8
    first = next(r for r in claimed if r['sha'] == source()['sha256'])
    db.save_result(first, {'revision': 1}, [])
    assert db.claim() is None  # Saved delivery remains leased until finish/retry.
    db.finish(first)
    next_job = db.claim()
    assert next_job['sha'] == first['sha'] and next_job['id'] != first['id']
    db.close()
    # Interrupted delivery keeps its structured result rather than redoing OCR.
    db = Database(tmp_path / 'state.sqlite')
    row = dict(db.db.execute('SELECT * FROM jobs WHERE id=?', (first['id'],)).fetchone())
    assert row['state'] == 'done'
    db.close()


def test_interrupted_saved_delivery_is_recovered(tmp_path):
    db = Database(tmp_path / 'state.sqlite')
    db.accept(batch(1))
    job = db.claim()
    db.save_result(job, {'revision': 7}, [])
    assert db.claim() is None
    db.close()
    db = Database(tmp_path / 'state.sqlite')
    recovered = db.claim()
    assert recovered['state'] == 'delivery'
    assert json.loads(recovered['result'])['revision'] == 7
    assert db.claim() is None
    db.close()


def test_resident_pool_reuses_process_and_recovers_timeout_and_crash(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    script = '''
import os,sys,json,time
from pathlib import Path
count=0
for line in sys.stdin:
    inp,out=json.loads(line)
    mode=Path(inp).read_text()
    if mode=='timeout': time.sleep(10)
    if mode=='crash': sys.exit(7)
    count+=1
    Path(out).write_text(json.dumps({'pid':os.getpid(),'count':count}))
'''
    monkeypatch.setattr('worker.pool.subprocess.Popen', lambda *a, **k: real_popen([sys.executable, '-u', '-c', script], **k))
    pool = RecognizerPool(1)
    inp, out = tmp_path/'input', tmp_path/'output'
    def parse(mode='ok', region='cn', timeout=5):
        inp.write_text(mode); out.unlink(missing_ok=True)
        pool.recognize(inp,out,region,timeout=timeout)
        return json.loads(out.read_text())
    try:
        first, second = parse(), parse()
        assert first['pid'] == second['pid'] and second['count'] == 2
        switched = parse(region='jp')
        assert switched['pid'] != first['pid'] and switched['count'] == 1
        with pytest.raises(TimeoutError): parse('timeout',region='jp',timeout=.05)
        assert parse()['count'] == 1
        with pytest.raises(RuntimeError,match='exited'): parse('crash')
        assert parse()['count'] == 1
    finally:
        pool.close()
    assert not pool.processes


def test_scheduler_overlaps_jobs_and_drains_on_shutdown(tmp_path):
    from dataclasses import replace
    db = Database(tmp_path/'state.sqlite')
    db.accept(batch(4))
    engine = Engine(replace(config(tmp_path),recognition_workers=2,prefetch_jobs=1), db)
    started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, peak = 0, 0
    engine.synchronize = lambda: None
    def process(job):
        nonlocal active, peak
        with lock:
            active += 1; peak=max(peak,active)
            if active >= 2: started.set()
        release.wait(5)
        db.finish(job)
        with lock: active -= 1
    engine.process = process
    async def run():
        task=asyncio.create_task(engine.run())
        try:
            assert await asyncio.to_thread(started.wait,5)
        finally:
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())
    assert peak >= 2 and active == 0
    assert db.db.execute('SELECT count(*) FROM jobs WHERE active=1').fetchone()[0] == 0
    db.close()


def test_two_resident_processes_execute_at_the_same_time(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    script = '''
import json,os,sys,time
from pathlib import Path
for line in sys.stdin:
    inp,out=json.loads(line)
    rendezvous=Path(inp).parent
    (rendezvous/(str(os.getpid())+'.started')).touch()
    deadline=time.monotonic()+5
    while len(list(rendezvous.glob('*.started')))<2:
        if time.monotonic()>deadline:sys.exit(9)
        time.sleep(.01)
    Path(out).write_text(json.dumps({'pid':os.getpid()}))
'''
    monkeypatch.setattr('worker.pool.subprocess.Popen',lambda *a,**k:real_popen([sys.executable,'-u','-c',script],**k))
    pool=RecognizerPool(2)
    def run(index):
        inp,out=tmp_path/f'{index}.in',tmp_path/f'{index}.out'
        inp.write_text('')
        pool.recognize(inp,out,'cn',timeout=8)
        return json.loads(out.read_text())['pid']
    try:
        with ThreadPoolExecutor(2) as executor:
            pids=list(executor.map(run,range(2)))
        assert len(set(pids))==2
    finally:
        pool.close()
