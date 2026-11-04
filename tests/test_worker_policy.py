"""Worker policy helpers: retry backoff, Retry-After parsing and worker identity (no database)."""

from datetime import UTC, datetime
from email.utils import format_datetime

from resolve.api import parse_retry_after
from resolve.worker import BACKOFF_MAX_S, backoff_s, default_owner


def test_backoff_honors_retry_after():
    assert backoff_s(0, 2.0) == 2.0
    assert backoff_s(5, 7.5) == 7.5


def test_server_retry_after_is_never_shortened():
    assert backoff_s(0, 120.0) == 120.0  # the cap applies to the local fallback only
    assert backoff_s(3, 600.0) == 600.0


def test_backoff_grows_with_jitter_and_cap():
    for retries, base in ((0, 1.0), (1, 2.0), (3, 8.0)):
        for _ in range(50):
            assert 0.8 * base <= backoff_s(retries, None) <= 1.2 * base
    assert all(backoff_s(20, None) <= BACKOFF_MAX_S * 1.2 for _ in range(50))


def test_backoff_ignores_nonpositive_retry_after():
    assert 0.8 <= backoff_s(0, 0.0) <= 1.2


def test_retry_after_delay_seconds():
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(" 5 ") == 5.0


def test_retry_after_http_date():
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC).timestamp()
    later = format_datetime(datetime(2026, 11, 10, 12, 1, 30, tzinfo=UTC), usegmt=True)
    assert parse_retry_after(later, now=now) == 90.0
    earlier = format_datetime(datetime(2026, 11, 10, 11, 0, 0, tzinfo=UTC), usegmt=True)
    assert parse_retry_after(earlier, now=now) == 0.0


def test_retry_after_absent_or_malformed():
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("soon") is None


def test_default_owner_is_unique_per_host_and_process():
    owner = default_owner()
    assert owner.startswith("worker-") and owner.count("-") >= 2
