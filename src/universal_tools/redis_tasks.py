"""Optional shared Tasks repository with atomic claims and explicit durability."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar

from .models import BrokerError, canonical_digest
from .tasks import _json, _timestamp

CREATE = """
if ARGV[12] ~= '' and redis.call('GET', KEYS[4]) ~= ARGV[12] then return {'error', 'lease'} end
local old = redis.call('GET', KEYS[1])
if old then
  if string.sub(old, 45) ~= ARGV[2] then return {'error', 'key_conflict'} end
  -- Give this connection a write offset before waiting for fsync.
  redis.call('SET', KEYS[1], old)
  return {'existing', string.sub(old, 1, 43)}
end
local count = tonumber(redis.call('HGET', KEYS[3], 'count') or '0')
local bytes = tonumber(redis.call('HGET', KEYS[3], 'bytes') or '0')
if count >= tonumber(ARGV[3]) or bytes + tonumber(ARGV[4]) > tonumber(ARGV[5]) then
  return {'error', 'quota'}
end
redis.call('HSET', KEYS[2], 'id', ARGV[1], 'fingerprint', ARGV[2],
  'owner', ARGV[6], 'route', ARGV[7], 'revision', ARGV[8],
  'created', ARGV[9], 'expires', ARGV[10], 'version', '0', 'data', ARGV[11])
redis.call('SET', KEYS[1], ARGV[1] .. ' ' .. ARGV[2])
redis.call('HINCRBY', KEYS[3], 'count', 1)
redis.call('HINCRBY', KEYS[3], 'bytes', ARGV[4])
return {'created', ARGV[1]}
"""

SAVE = """
if ARGV[5] ~= '' and redis.call('GET', KEYS[3]) ~= ARGV[5] then return 'lease' end
local version = redis.call('HGET', KEYS[1], 'version')
if not version or version ~= ARGV[1] then return 'conflict' end
if tonumber(redis.call('HGET', KEYS[1], 'expires')) <= tonumber(ARGV[4]) then return 'expired' end
local old = redis.call('HGET', KEYS[1], 'data')
local change = string.len(ARGV[2]) - string.len(old)
local bytes = tonumber(redis.call('HGET', KEYS[2], 'bytes') or '0')
if bytes + change > tonumber(ARGV[3]) then return 'quota' end
redis.call('HSET', KEYS[1], 'data', ARGV[2])
redis.call('HINCRBY', KEYS[1], 'version', 1)
redis.call('HINCRBY', KEYS[2], 'bytes', change)
return 'saved'
"""

UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


class RedisTaskStore:
    """redis.asyncio-compatible shared state; never uses key TTL or eviction.

    Redis must use AOF, noeviction and access controls. By default WAITAOF
    confirms local fsync and the configured number of replicas before effects.
    A deployment may inject an equivalent durable barrier. A failed barrier
    never permits an app start/update/cancel; its intent remains for recovery.
    """

    def __init__(
        self,
        client,
        namespace,
        *,
        capacity=10_000,
        max_storage_bytes=64 * 1024 * 1024,
        replicas=0,
        durable=None,
    ):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", namespace):
            raise ValueError("invalid Redis task namespace")
        if not 1 <= capacity <= 1_000_000 or not 0 <= replicas <= 10:
            raise ValueError("invalid Redis task limits")
        self.client = client
        self.prefix = "mcp:{" + namespace + "}:"
        self.capacity, self.max_storage_bytes = capacity, max_storage_bytes
        self.replicas, self.durable = replicas, durable
        self._lease = ContextVar("redis_task_operation_lease", default=None)

    @asynccontextmanager
    async def guard(self, operation_timeout):
        # Namespace serialization keeps inspect/reconciliation from racing a
        # not-yet-dispatched creation or approval intent on another replica.
        key, value = self.prefix + "operation-lease", secrets.token_urlsafe(32)
        ttl = operation_timeout * 8 + 5
        acquired = await self.client.set(key, value, nx=True, px=int(ttl * 1000))
        if not acquired:
            raise BrokerError("shared task repository is busy; poll again later")
        token = self._lease.set(value)
        try:
            async with asyncio.timeout(ttl - 5):
                yield
        finally:
            self._lease.reset(token)
            # Never release a newer owner's lease, including after process pause.
            try:
                async with asyncio.timeout(5):
                    await self.client.eval(UNLOCK, 1, key, value)
            except Exception:
                pass  # The bounded lease expires; no operation is replayed.

    async def check_guard(self):
        value = self._lease.get()
        if value is None or text(await self.client.get(self.prefix + "operation-lease")) != value:
            raise BrokerError("shared task lease lost; reconcile before another effect")

    async def _barrier(self, connection):
        if self.durable:
            await self.durable()
            return
        try:
            result = await connection.execute_command("WAITAOF", 1, self.replicas, 2000)
            if result[0] < 1 or result[1] < self.replicas:
                raise ValueError
        except Exception:
            raise BrokerError("task persistence unconfirmed; reconcile without replay") from None

    async def create(self, owner, request_key, route, revision, arguments, ttl_ms):
        fingerprint = canonical_digest([route, revision, arguments])
        task_id = secrets.token_urlsafe(32)
        stamp = _timestamp()
        raw = _json(
            {
                "wire": {
                    "taskId": task_id,
                    "status": "working",
                    "createdAt": stamp,
                    "lastUpdatedAt": stamp,
                    "ttlMs": ttl_ms,
                    "pollIntervalMs": 1000,
                },
                "handle": None,
                "pending": {"kind": "start"},
                "seen": {},
                "answered": [],
            }
        )
        created = time.time()
        operation = hashlib.sha256((owner + "\0" + request_key).encode()).hexdigest()
        async with self.client.client() as connection:
            result = await connection.eval(
                CREATE,
                4,
                self.prefix + "key:" + operation,
                self.prefix + "task:" + task_id,
                self.prefix + "quota",
                self.prefix + "operation-lease",
                task_id,
                fingerprint,
                self.capacity,
                len(raw.encode()) + 2048,
                self.max_storage_bytes,
                owner,
                route,
                revision,
                created,
                created + ttl_ms / 1000,
                raw,
                self._lease.get() or "",
            )
            await self._barrier(connection)
        kind, identifier = map(text, result)
        if kind == "error":
            raise BrokerError("task operation key conflicts or the shared store quota is exhausted")
        # Also barrier existing claims: a previous client may have lost the
        # durability acknowledgment. Existing intents are never dispatched here.
        return identifier, kind == "created"

    async def load(self, task_id, owner):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", task_id):
            raise BrokerError("unknown or expired task")
        result = await self.client.hgetall(self.prefix + "task:" + task_id)
        record = {text(k): text(v) for k, v in result.items()}
        if not record or record["owner"] != owner or float(record["expires"]) <= time.time():
            raise BrokerError("unknown or expired task")
        record["version"] = int(record["version"])
        record["data"] = json.loads(record["data"])
        return record

    async def save(self, record):
        raw = _json(record["data"])
        async with self.client.client() as connection:
            status = text(
                await connection.eval(
                    SAVE,
                    3,
                    self.prefix + "task:" + record["id"],
                    self.prefix + "quota",
                    self.prefix + "operation-lease",
                    record["version"],
                    raw,
                    self.max_storage_bytes,
                    time.time(),
                    self._lease.get() or "",
                )
            )
            if status == "saved":
                await self._barrier(connection)
        if status != "saved":
            raise BrokerError("task changed, expired or exceeded quota; poll before another action")
        record["version"] += 1
