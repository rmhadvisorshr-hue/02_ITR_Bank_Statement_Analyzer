"""Progress channel abstraction: "the events published so far for job X,
plus any new ones as they arrive." Backed by Redis Streams when REDIS_URL is
configured, or an in-process buffer when it isn't. main.py's POST /analyze
(producer) and GET /analyze/{job_id}/stream (consumer) only ever call
write()/read_from_start() - neither needs to know which backend is active,
which is what lets this app run identically with or without Redis.
"""
import asyncio
import json
import os
from typing import AsyncIterator

REDIS_URL = os.environ.get("REDIS_URL")

TERMINAL_TYPES = {"done", "error"}
STREAM_TTL_SECONDS = 3600  # plenty of time for a client to finish reading


class RedisStreamChannel:
    def __init__(self, job_id: str):
        self._key = f"analysis-stream:{job_id}"
        self._redis = _get_redis_client()

    async def write(self, event: dict) -> None:
        await self._redis.xadd(self._key, {"data": json.dumps(event)})
        if event.get("type") in TERMINAL_TYPES:
            await self._redis.expire(self._key, STREAM_TTL_SECONDS)

    async def read_from_start(self) -> AsyncIterator[dict]:
        last_id = "0"
        while True:
            # Blocks up to 30s waiting for new entries rather than busy-polling;
            # a bare timeout (no entries) just loops back and blocks again.
            response = await self._redis.xread({self._key: last_id}, block=30000, count=100)
            for _key, messages in response:
                for message_id, fields in messages:
                    last_id = message_id
                    event = json.loads(fields["data"])
                    yield event
                    if event.get("type") in TERMINAL_TYPES:
                        return


_redis_client = None


def _get_redis_client():
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as redis_asyncio
        _redis_client = redis_asyncio.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


class InProcessChannel:
    """Fallback used when REDIS_URL isn't set: an append-only event log plus
    a condition variable, so a reader that attaches after some events were
    already written still replays them before waiting for new ones."""

    def __init__(self):
        self._events: list[dict] = []
        self._condition = asyncio.Condition()

    async def write(self, event: dict) -> None:
        async with self._condition:
            self._events.append(event)
            self._condition.notify_all()

    async def read_from_start(self) -> AsyncIterator[dict]:
        i = 0
        while True:
            async with self._condition:
                while i >= len(self._events):
                    await self._condition.wait()
                event = self._events[i]
                i += 1
            yield event
            if event.get("type") in TERMINAL_TYPES:
                return


_in_process_channels: dict[str, InProcessChannel] = {}


def get_channel(job_id: str):
    if REDIS_URL:
        return RedisStreamChannel(job_id)
    if job_id not in _in_process_channels:
        _in_process_channels[job_id] = InProcessChannel()
    return _in_process_channels[job_id]
