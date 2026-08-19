"""Redis KVStore + LockProvider (ADR-0004 D3).

Hot state for the multi-process form: warm claims, route entries, leases.
Every key is namespaced by a configurable prefix so one Redis can host
multiple deployments. CAS is a single atomic Lua script (GET-compare-SET);
lock release is token-checked so only the holder deletes — stricter than
the in-process implementation's blind release, and the strengthening is
intentional (ADR-0004 D3).

Requires the `whirlwind[redis]` extra. Import this module directly; it is
not re-exported from `whirlwind.storage` so the base package never needs
the redis client.
"""

from __future__ import annotations

import uuid

from redis import asyncio as aioredis

# ARGV[1]: "1" when expected is None (match missing key), else "0"
# ARGV[2]: expected value ("" when None)
# ARGV[3]: new value
# Success clears any TTL — parity with the in-process CAS.
_CAS_LUA = """
if ARGV[1] == '1' then
  if redis.call('GET', KEYS[1]) == false then
    redis.call('SET', KEYS[1], ARGV[3])
    return 1
  end
  return 0
end
if redis.call('GET', KEYS[1]) == ARGV[2] then
  redis.call('SET', KEYS[1], ARGV[3])
  return 1
end
return 0
"""

_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
"""


class RedisKVStore:
    """Satisfies the `KVStore` Protocol against a real Redis."""

    def __init__(self, url: str, *, prefix: str = "wh:kv:") -> None:
        self.url = url
        self.prefix = prefix
        self.client: aioredis.Redis | None = None
        self._cas = None

    async def start(self) -> None:
        """Connect and ping (fail fast on a bad URL)."""
        if self.client is not None:
            return
        self.client = aioredis.from_url(self.url, decode_responses=True)
        await self.client.ping()
        self._cas = self.client.register_script(_CAS_LUA)

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None
            self._cas = None

    @property
    def _redis(self) -> aioredis.Redis:
        if self.client is None:
            raise RuntimeError("RedisKVStore not started; call start() first")
        return self.client

    def _key(self, key: str) -> str:
        return self.prefix + key

    async def get(self, key: str) -> str | None:
        return await self._redis.get(self._key(key))

    async def put(self, key: str, value: str, *, ttl_s: float | None = None) -> None:
        k = self._key(key)
        if ttl_s is None:
            await self._redis.set(k, value)  # plain SET: overwrites clear any TTL
        else:
            await self._redis.set(k, value, px=int(ttl_s * 1000))

    async def delete(self, key: str) -> None:
        await self._redis.delete(self._key(key))

    async def cas(self, key: str, expected: str | None, new: str) -> bool:
        result = await self._cas(
            keys=[self._key(key)],
            args=["1" if expected is None else "0", expected or "", new],
        )
        return bool(int(result))


class RedisLocks:
    """Satisfies the `LockProvider` Protocol against a real Redis.

    `SET NX PX` acquisition with a per-holder token; release deletes only
    when the stored token still matches (TTL expiry between acquire and
    release can never unlock someone else's lock).
    """

    def __init__(self, url: str, *, prefix: str = "wh:lock:") -> None:
        self.url = url
        self.prefix = prefix
        self.client: aioredis.Redis | None = None
        self._tokens: dict[str, str] = {}
        self._release = None

    async def start(self) -> None:
        if self.client is not None:
            return
        self.client = aioredis.from_url(self.url, decode_responses=True)
        await self.client.ping()
        self._release = self.client.register_script(_RELEASE_LOCK_LUA)

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None
            self._release = None
            self._tokens.clear()

    async def acquire(self, name: str, ttl_s: float = 30.0) -> bool:
        token = uuid.uuid4().hex
        ok = await self.client.set(self.prefix + name, token, nx=True, px=int(ttl_s * 1000))
        if not ok:
            return False
        self._tokens[name] = token
        return True

    async def release(self, name: str) -> None:
        token = self._tokens.pop(name, None)
        if token is None:
            return  # this instance does not hold the lock; nothing to release
        await self._release(keys=[self.prefix + name], args=[token])
