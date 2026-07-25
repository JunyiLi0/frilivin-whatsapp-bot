"""Quota boundaries — the exact counts matter, so they are pinned here."""

from __future__ import annotations

import fakeredis

from whatsapp_bot.ratelimit import RateLimiter

# 2026-07-25T12:00:30Z, comfortably inside a minute.
NOON = 1784980830.0
MINUTE = 60.0
DAY = 86400.0


def build(redis_conn: fakeredis.FakeRedis, per_minute: int, per_day: int) -> RateLimiter:
    return RateLimiter(redis_conn, per_minute=per_minute, per_day=per_day)


def test_allows_up_to_the_minute_limit(redis_conn: fakeredis.FakeRedis) -> None:
    limiter = build(redis_conn, per_minute=30, per_day=500)

    verdicts = [limiter.check_and_consume(NOON) for _ in range(30)]

    assert all(verdict.allowed for verdict in verdicts)
    assert verdicts[-1].minute_count == 30


def test_refuses_the_one_past_the_minute_limit(redis_conn: fakeredis.FakeRedis) -> None:
    limiter = build(redis_conn, per_minute=30, per_day=500)
    for _ in range(30):
        limiter.check_and_consume(NOON)

    verdict = limiter.check_and_consume(NOON)

    assert verdict.allowed is False
    assert verdict.scope == "minute"
    assert "minute" in verdict.reason


def test_a_refused_send_does_not_consume_quota(redis_conn: fakeredis.FakeRedis) -> None:
    """Otherwise a burst would keep pushing the limit further out of reach."""
    limiter = build(redis_conn, per_minute=2, per_day=500)
    limiter.check_and_consume(NOON)
    limiter.check_and_consume(NOON)

    refused = [limiter.check_and_consume(NOON) for _ in range(5)]

    assert all(not verdict.allowed for verdict in refused)
    assert all(verdict.minute_count == 2 for verdict in refused)
    assert refused[-1].day_count == 2


def test_the_next_minute_starts_fresh(redis_conn: fakeredis.FakeRedis) -> None:
    limiter = build(redis_conn, per_minute=2, per_day=500)
    limiter.check_and_consume(NOON)
    limiter.check_and_consume(NOON)
    assert limiter.check_and_consume(NOON).allowed is False

    verdict = limiter.check_and_consume(NOON + MINUTE)

    assert verdict.allowed is True
    assert verdict.minute_count == 1
    # The daily counter keeps running across minutes.
    assert verdict.day_count == 3


def test_the_daily_limit_wins_over_the_minute_one(redis_conn: fakeredis.FakeRedis) -> None:
    limiter = build(redis_conn, per_minute=100, per_day=3)
    now = NOON
    for index in range(3):
        assert limiter.check_and_consume(now + index * MINUTE).allowed is True

    verdict = limiter.check_and_consume(now + 3 * MINUTE)

    assert verdict.allowed is False
    assert verdict.scope == "day"


def test_the_next_day_starts_fresh(redis_conn: fakeredis.FakeRedis) -> None:
    limiter = build(redis_conn, per_minute=100, per_day=2)
    limiter.check_and_consume(NOON)
    limiter.check_and_consume(NOON)
    assert limiter.check_and_consume(NOON).allowed is False

    verdict = limiter.check_and_consume(NOON + DAY)

    assert verdict.allowed is True
    assert verdict.day_count == 1


def test_counters_expire_so_redis_stays_small(redis_conn: fakeredis.FakeRedis) -> None:
    limiter = build(redis_conn, per_minute=30, per_day=500)
    limiter.check_and_consume(NOON)

    minute_key, day_key = limiter.keys(NOON)
    assert 0 < redis_conn.ttl(minute_key) <= 120
    assert 0 < redis_conn.ttl(day_key) <= 172800


def test_keys_are_derived_from_utc(redis_conn: fakeredis.FakeRedis) -> None:
    limiter = build(redis_conn, per_minute=1, per_day=1)
    minute_key, day_key = limiter.keys(NOON)

    assert minute_key == "rl:min:202607251200"
    assert day_key == "rl:day:20260725"


def test_two_limiters_share_the_same_quota(redis_conn: fakeredis.FakeRedis) -> None:
    """The api and the worker are different processes but one bot."""
    first = build(redis_conn, per_minute=2, per_day=500)
    second = build(redis_conn, per_minute=2, per_day=500)

    assert first.check_and_consume(NOON).allowed is True
    assert second.check_and_consume(NOON).allowed is True
    assert first.check_and_consume(NOON).allowed is False
