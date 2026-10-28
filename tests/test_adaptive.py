import json

from worker.adaptive import Adaptive
from worker.db import Database
from worker.engine import Engine
from test_worker import batch, config


def test_automatic_limits_follow_hardware_pressure_and_cloud_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr('worker.adaptive.hardware', lambda: (10, 8 * 1024**3))
    policy = Adaptive(config(tmp_path))
    assert policy.maximum['recognize'] == 10
    initial = policy.limit('recognize')
    policy.adjust({'verified': 10}, {'cpu': .3, 'memory': .4, 'host_cpu': .3})
    assert policy.limit('recognize') == initial + 1
    policy.adjust({'verified': 10}, {'cpu': .9, 'memory': .9, 'host_cpu': .9})
    assert policy.limit('recognize') == initial
    previous = policy.limit('download')
    policy.throttled(20)
    assert policy.limit('download') == max(1, previous // 2)
    assert not policy.download_ready()
    policy.throttled(20)  # A burst of simultaneous 429s reduces only once.
    assert policy.snapshot()['download_throttles'] == 1


def test_recognition_deletes_originals_before_delivery_and_reuses_revision(tmp_path):
    db = Database(tmp_path / 'state.sqlite')
    db.accept(batch(1))
    engine = Engine(config(tmp_path), db)

    class Stats:
        queries = 0
        offline = True

        def revision(self, sha):
            self.queries += 1
            return 6

        def post(self, path, body):
            if self.offline:
                raise RuntimeError('C unavailable')
            return {'receipts': [{'status': 'accepted'}]}

    original_client = engine.stats.client
    engine.stats = Stats()
    engine.run_recognizer = lambda *args: ({'status': 'complete', 'events': [], 'diagnostics': [],
                                           'candidates': [], 'used_templates': []}, .01)
    try:
        job = db.claim_stage('pending')
        directory = engine.workdir(job)
        directory.mkdir()
        (directory / '0.png').write_bytes(b'original')
        engine.stage_recognize(job)
        assert engine.stats.queries == 0
        assert not directory.exists()
        job = db.claim_stage('delivery')
        engine.stage_deliver(job)
        row = dict(db.db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
        assert json.loads(row['result'])['revision'] == 7 and row['state'] == 'delivery'
        with db.db:
            db.db.execute('UPDATE jobs SET retry_at=0')
        engine.stats.offline = False
        engine.stage_deliver(db.claim_stage('delivery'))
        assert engine.stats.queries == 1 and db.pending() == 0
    finally:
        for executor in (*engine.executors.values(), engine.scheduler, engine.management):
            executor.shutdown()
        original_client.close()
        engine.pan.client.close()
        db.close()


def test_restart_redownloads_intermediate_files_but_preserves_saved_results(tmp_path):
    db = Database(tmp_path / 'state.sqlite')
    db.accept(batch(2))
    first = db.claim_stage('pending')
    db.advance(first, 'verified')
    second = db.claim_stage('pending')
    db.save_result(second, {'revision': 4}, [])
    db.close()
    db = Database(tmp_path / 'state.sqlite')
    try:
        assert db.claim_stage('pending')['id'] == first['id']
        assert json.loads(db.claim_stage('delivery')['result'])['revision'] == 4
    finally:
        db.close()
