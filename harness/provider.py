"""Meta Model API wrapper. SDK retries are off; this wrapper owns retries.

| Provider response            | Action                                          |
|------------------------------|-------------------------------------------------|
| 429                          | wait Retry-After (or back off), up to 3 retries |
| 500, 503                     | honor Retry-After or back off, up to 3 retries  |
| 504, 400, anything else      | no retry -> ProviderFailed                      |
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass

import openai

META_BASE_URL = "https://api.meta.ai/v1"
RETRYABLE = {429, 500, 503}
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
RETRY_AFTER_CAP_S = 60.0

# USD per 1M tokens: (uncached input, cached input, output incl. reasoning). the design.
PRICES = {
    "muse-spark-1.3": (1.25, 0.15, 4.25),
    "muse-spark-1.3-contributor": (1.25, 0.15, 4.25),
    "muse-wonder-1.0": (4.00, 0.20, 20.00),
}


def cost_usd(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    p_in, p_cached, p_out = PRICES[model]
    return (p_in * (input_tokens - cached_tokens) + p_cached * cached_tokens + p_out * output_tokens) / 1e6


def make_client(timeout_s: float = 60.0) -> openai.AsyncOpenAI:
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise SystemExit("MODEL_API_KEY is not set")
    return openai.AsyncOpenAI(base_url=META_BASE_URL, api_key=key, max_retries=0, timeout=timeout_s)


class ProviderFailed(Exception):
    def __init__(self, status: int | str, message: str, retries: int, wait_s: float):
        super().__init__(f"{status}: {message}")
        self.status, self.message, self.retries, self.wait_s = status, message, retries, wait_s


@dataclass
class ProviderResult:
    response: object
    latency_ms: float  # of the successful attempt
    retries: int
    wait_s: float


def _retry_after(exc: openai.APIStatusError) -> float | None:
    value = exc.response.headers.get("retry-after") if exc.response is not None else None
    try:
        return min(float(value), RETRY_AFTER_CAP_S) if value is not None else None
    except ValueError:
        return None


def _message(exc: openai.APIStatusError) -> str:
    body = exc.body if isinstance(exc.body, dict) else {}
    err = body.get("error", body)
    if isinstance(err, dict):
        return str(err.get("code") or err.get("type") or "") + " " + str(err.get("message", ""))[:300]
    return str(exc)[:300]


class Provider:
    def __init__(self, client, max_retries: int = 3, sleep=asyncio.sleep, rng: random.Random | None = None):
        self.client = client
        self.max_retries = max_retries
        self.sleep = sleep
        self.rng = rng or random.Random()

    async def create(self, **kwargs) -> ProviderResult:
        retries, wait_total = 0, 0.0
        while True:
            t0 = time.perf_counter()
            try:
                response = await self.client.responses.create(**kwargs)
                return ProviderResult(response, (time.perf_counter() - t0) * 1000, retries, wait_total)
            except openai.APIStatusError as exc:
                if exc.status_code not in RETRYABLE or retries >= self.max_retries:
                    raise ProviderFailed(exc.status_code, _message(exc), retries, wait_total) from exc
                wait = _retry_after(exc)
                if wait is None:  # full-jitter exponential backoff
                    wait = self.rng.uniform(0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**retries))
            except (openai.APITimeoutError, openai.APIConnectionError) as exc:
                raise ProviderFailed(type(exc).__name__, str(exc)[:300], retries, wait_total) from exc
            retries += 1
            wait_total += wait
            await self.sleep(wait)
