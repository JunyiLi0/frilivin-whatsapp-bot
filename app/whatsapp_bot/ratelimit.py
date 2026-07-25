"""Global outbound rate limiter — N per minute and M per day, all handlers included.

The check and the increment happen inside a single Lua script so they are
atomic. A naive "GET, compare, INCR" would count refused sends against the
quota under concurrency, progressively starving the bot.
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple

from redis import Redis

# KEYS[1] minute counter, KEYS[2] day counter
# ARGV[1] per-minute limit, ARGV[2] per-day limit,
# ARGV[3] minute TTL,       ARGV[4] day TTL
_LUA_CONSUME = """
local minute = tonumber(redis.call('GET', KEYS[1]) or '0')
local day    = tonumber(redis.call('GET', KEYS[2]) or '0')

if minute >= tonumber(ARGV[1]) then
    return {0, 'minute', minute, day}
end
if day >= tonumber(ARGV[2]) then
    return {0, 'day', minute, day}
end

minute = redis.call('INCR', KEYS[1])
if minute == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[3])
end

day = redis.call('INCR', KEYS[2])
if day == 1 then
    redis.call('EXPIRE', KEYS[2], ARGV[4])
end

return {1, 'ok', minute, day}
"""

_MINUTE_TTL = 120
_DAY_TTL = 172800  # 48 h: survives a clock skew or a late-night restart


class RateLimitResult(NamedTuple):
    allowed: bool
    scope: str  # "ok", "minute" or "day"
    minute_count: int
    day_count: int

    @property
    def reason(self) -> str:
        if self.allowed:
            return "ok"
        return f"{self.scope} quota exhausted"


def _as_str(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class RateLimiter:
    def __init__(
        self,
        connection: Redis,
        *,
        per_minute: int,
        per_day: int,
        prefix: str = "rl",
    ) -> None:
        self._per_minute = per_minute
        self._per_day = per_day
        self._prefix = prefix
        self._script = connection.register_script(_LUA_CONSUME)

    def keys(self, now: float | None = None) -> tuple[str, str]:
        stamp = time.gmtime(now if now is not None else time.time())
        minute_key = f"{self._prefix}:min:{time.strftime('%Y%m%d%H%M', stamp)}"
        day_key = f"{self._prefix}:day:{time.strftime('%Y%m%d', stamp)}"
        return minute_key, day_key

    def check_and_consume(self, now: float | None = None) -> RateLimitResult:
        """Consume one send slot. Returns whether it was granted."""
        minute_key, day_key = self.keys(now)
        raw: Any = self._script(
            keys=[minute_key, day_key],
            args=[self._per_minute, self._per_day, _MINUTE_TTL, _DAY_TTL],
        )
        allowed = int(raw[0]) == 1
        return RateLimitResult(
            allowed=allowed,
            scope=_as_str(raw[1]),
            minute_count=int(raw[2]),
            day_count=int(raw[3]),
        )
