"""Чистый разбор окон ограничений и экспоненты."""

import pytest

from astra_voice.net.http import backoff_seconds, parse_rate_limit, parse_retry_after

pytestmark = pytest.mark.unit


def test_retry_after() -> None:
    assert parse_retry_after("20", 0) == 20
    assert parse_retry_after("-1", 0) is None
    assert parse_retry_after("bad", 0) is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 2_000_000_000) == 0
    assert parse_retry_after("999999", 0) == 86400


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1030"}, 30),
        ({"RateLimit": "limit=60, remaining=0, reset=30"}, 30),
        ({"RateLimit": '"api";r=0;t=55'}, 55),
        ({"RateLimit": '"burst";r=5;t=1, "api";r=0;t=55'}, 55),
        ({"RateLimit": "limit=60, remaining=1, reset=30"}, None),
        ({"RateLimit": "limit=60, remaining=0, reset=999999"}, 86400),
    ],
)
def test_rate_limit(headers: dict[str, str], expected: float | None) -> None:
    assert parse_rate_limit(headers, 1000) == expected


def test_backoff() -> None:
    assert [backoff_seconds(n) for n in (1, 2, 3, 100)] == [300, 600, 1200, 86400]
