import asyncio
from dataclasses import replace
import json

from fastapi.testclient import TestClient
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from worker.app import create_app
from worker.db import Database
from worker.uploader import UploaderConnection
from test_worker import batch, config


def test_reverse_connection_replays_without_duplicate_jobs(tmp_path):
    async def scenario():
        db = Database(tmp_path / "state.sqlite")
        deliveries = 0
        accepted = asyncio.Event()
        packet = batch(1)
        packet.update(window_start="2026-10-27T01:00:00Z", window_end="2026-10-27T01:30:00Z")

        async def uploader(ws):
            nonlocal deliveries
            assert ws.request.headers["Authorization"] == "Bearer worker-secret"
            deliveries += 1
            await ws.send(json.dumps(packet))
            if deliveries == 1:
                # Simulate A losing its socket before observing the durable ACK.
                while db.pending() == 0:
                    await asyncio.sleep(0.005)
                await ws.close()
            else:
                assert json.loads(await ws.recv()) == {"type": "batch_accepted", "batch_id": packet["batch_id"]}
                accepted.set()
                await ws.wait_closed()

        async with serve(uploader, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            c = replace(config(tmp_path), uploader_url=f"ws://127.0.0.1:{port}/internal/v1/worker/connect")
            connection = UploaderConnection(c, db)
            connection.retry_delay = 0.01
            task = asyncio.create_task(connection.run())
            try:
                await asyncio.wait_for(accepted.wait(), 5)
                assert connection.connected
                assert db.pending() == 1
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            assert not connection.connected
        db.close()
        restored = Database(tmp_path / "state.sqlite")
        assert restored.pending() == 1
        restored.close()

    asyncio.run(scenario())


def test_reverse_invalid_batch_is_never_acknowledged(tmp_path):
    async def scenario():
        db = Database(tmp_path / "state.sqlite")
        closed = asyncio.Event()

        async def uploader(ws):
            await ws.send('{"type":"batch_ready","protocol_version":1,"batch_id":"invalid","items":[]}')
            try:
                message = await asyncio.wait_for(ws.recv(), 2)
                raise AssertionError(f"Invalid batch acknowledged: {message}")
            except ConnectionClosed:
                closed.set()

        async with serve(uploader, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            c = replace(config(tmp_path), uploader_url=f"ws://127.0.0.1:{port}/internal/v1/worker/connect")
            task = asyncio.create_task(UploaderConnection(c, db).run())
            try:
                await asyncio.wait_for(closed.wait(), 5)
                assert db.pending() == 0
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        db.close()

    asyncio.run(scenario())


def test_unreachable_uploader_does_not_block_health_or_shutdown(tmp_path):
    c = replace(config(tmp_path), uploader_url="ws://127.0.0.1:1/internal/v1/worker/connect")
    with TestClient(create_app(c, start_engine=False)) as client:
        result = client.get("/healthz")
        assert result.status_code == 200
        assert result.json()["uploader_connected"] is False
