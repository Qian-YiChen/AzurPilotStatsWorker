"""B opens the connection; A sends durable batches over the same socket."""
import asyncio
import json
import logging

from websockets.asyncio.client import connect

log = logging.getLogger(__name__)


async def accept_message(db, message):
    batch = json.loads(message)
    await asyncio.to_thread(db.accept, batch)
    return json.dumps({"type": "batch_accepted", "batch_id": batch["batch_id"]})


class UploaderConnection:
    def __init__(self, config, db):
        self.config, self.db = config, db
        self.connected = False
        self.retry_delay = 5

    async def run(self):
        while True:
            try:
                async with connect(
                    self.config.uploader_url,
                    additional_headers={"Authorization": "Bearer " + self.config.worker_token},
                    proxy=None,
                    max_size=16 * 1024**2,
                    max_queue=1,
                    compression=None,
                    open_timeout=15,
                    ping_interval=25,
                    ping_timeout=10,
                ) as ws:
                    self.connected = True
                    log.info("Connected to uploader; waiting for scheduled batches")
                    async for message in ws:
                        await ws.send(await accept_message(self.db, message))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Network exception strings may contain URLs; credentials never
                # belong in logs. Acknowledgement follows the SQLite commit.
                log.warning("Uploader connection interrupted (%s); retry in 5s", type(exc).__name__)
            finally:
                self.connected = False
            await asyncio.sleep(self.retry_delay)
