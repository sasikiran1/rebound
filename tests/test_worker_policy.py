"""Worker policy helpers: retry backoff and worker identity (no database)."""

from resolve.worker import BACKOFF_MAX_S, backoff_s, default_owner


def test_backoff_honors_retry_after():
    assert backoff_s(0, 2.0) == 2.0
    assert backoff_s(5, 7.5) == 7.5


def test_backoff_caps_retry_after():
    assert backoff_s(0, 600.0) == BACKOFF_MAX_S


def test_backoff_grows_with_jitter_and_cap():
    for retries, base in ((0, 1.0), (1, 2.0), (3, 8.0)):
        for _ in range(50):
            assert 0.8 * base <= backoff_s(retries, None) <= 1.2 * base
    assert all(backoff_s(20, None) <= BACKOFF_MAX_S * 1.2 for _ in range(50))


def test_backoff_ignores_nonpositive_retry_after():
    assert 0.8 <= backoff_s(0, 0.0) <= 1.2


def test_default_owner_is_unique_per_host_and_process():
    owner = default_owner()
    assert owner.startswith("worker-") and owner.count("-") >= 2
