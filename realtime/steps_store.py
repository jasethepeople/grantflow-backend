"""
Storage for live ProseMirror collaborative-editing steps, backed by Redis.

Follows the same model as prosemirror-collab's reference server: a per-section
ordered list of (step, clientID) entries. `version` is simply the length of
that list. A client submits steps along with the version it started from;
if the store's current version doesn't match, the submission is rejected and
the client is expected to fetch the steps it's missing, rebase locally, and
resubmit — exactly like the official protocol.

The compare-and-append is done in a single Lua script so it's atomic even
with multiple consumer processes talking to the same Redis instance —
no separate WATCH/MULTI round trip needed.
"""

from __future__ import annotations

import json

import redis.asyncio as aioredis
from django.conf import settings

STEP_TTL_SECONDS = 60 * 60 * 24 * 7  # abandoned collab sessions expire after a week

_SUBMIT_SCRIPT = """
local key = KEYS[1]
local expected_version = tonumber(ARGV[1])
local entries_json = ARGV[2]
local ttl = tonumber(ARGV[3])

local current_version = redis.call('LLEN', key)
if current_version ~= expected_version then
    return {0, current_version}
end

local entries = cjson.decode(entries_json)
for i = 1, #entries do
    redis.call('RPUSH', key, cjson.encode(entries[i]))
end
redis.call('EXPIRE', key, ttl)
return {1, current_version + #entries}
"""

_clients_by_loop: dict = {}


def _get_client():
    import asyncio

    loop = asyncio.get_event_loop()
    client = _clients_by_loop.get(loop)
    if client is None:
        redis_url = getattr(settings, "REDIS_URL", "redis://localhost:6379/0")
        client = aioredis.from_url(redis_url, decode_responses=True)
        _clients_by_loop[loop] = client
    return client


def _steps_key(section_id) -> str:
    return f"section:{section_id}:collab:steps"


async def get_version(section_id) -> int:
    client = _get_client()
    return await client.llen(_steps_key(section_id))


async def get_steps_since(section_id, from_version: int) -> list[dict]:
    """Returns entries (each {"step": ..., "client_id": ...}) submitted after from_version."""
    client = _get_client()
    raw_entries = await client.lrange(_steps_key(section_id), from_version, -1)
    return [json.loads(entry) for entry in raw_entries]


async def submit_steps(section_id, expected_version: int, entries: list[dict]) -> tuple[bool, int]:
    """
    Atomically appends `entries` (list of {"step": ..., "client_id": ...}) if the store's
    current version equals expected_version. Returns (success, resulting_version) — on
    failure, resulting_version is the store's actual current version so the caller can
    fetch the steps it's missing.
    """
    client = _get_client()
    key = _steps_key(section_id)
    result = await client.eval(
        _SUBMIT_SCRIPT, 1, key, expected_version, json.dumps(entries), STEP_TTL_SECONDS,
    )
    success_flag, resulting_version = result
    return bool(success_flag), int(resulting_version)


async def reset(section_id) -> None:
    """
    Clears all pending collab steps for a section. Called whenever the section's
    baseline content changes via the REST version-control endpoints (a human save
    or an AI generation), since those steps were relative to the old baseline.
    """
    client = _get_client()
    await client.delete(_steps_key(section_id))


def reset_sync(section_id) -> None:
    """Synchronous variant for use from regular (non-async) DRF views."""
    import redis as sync_redis

    redis_url = getattr(settings, "REDIS_URL", "redis://localhost:6379/0")
    client = sync_redis.from_url(redis_url, decode_responses=True)
    client.delete(_steps_key(section_id))
    client.close()
