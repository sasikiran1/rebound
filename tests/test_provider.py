import httpx
import openai
import pytest

from harness.provider import Provider, ProviderFailed, cost_usd


def status_error(code: int, headers: dict | None = None, body: dict | None = None) -> openai.APIStatusError:
    req = httpx.Request("POST", "https://api.meta.ai/v1/responses")
    resp = httpx.Response(code, headers=headers or {}, json=body or {}, request=req)
    cls = {400: openai.BadRequestError, 429: openai.RateLimitError, 500: openai.InternalServerError}.get(
        code, openai.APIStatusError
    )
    return cls("err", response=resp, body=body)


class FakeResponses:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script):
        self.responses = FakeResponses(script)


def provider(script):
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    client = FakeClient(script)
    return Provider(client, sleep=fake_sleep), client, waits


async def test_429_honors_retry_after_then_succeeds():
    p, client, waits = provider([status_error(429, {"Retry-After": "2"}), "ok"])
    res = await p.create()
    assert res.response == "ok" and res.retries == 1 and waits == [2.0] and res.wait_s == 2.0


async def test_503_retried_at_most_three_times():
    errors = [status_error(503, body={"error": {"code": "service_overloaded"}}) for _ in range(4)]
    p, client, waits = provider(errors)
    with pytest.raises(ProviderFailed) as info:
        await p.create()
    assert client.responses.calls == 4 and len(waits) == 3 and info.value.retries == 3


@pytest.mark.parametrize("code", [400, 504])
async def test_no_retry_codes(code):
    p, client, waits = provider([status_error(code, body={"error": {"message": "bad"}})])
    with pytest.raises(ProviderFailed) as info:
        await p.create()
    assert client.responses.calls == 1 and waits == [] and info.value.status == code


def test_spark_cost_formula():
    # 10k input of which 4k cached, 1k output: (1.25*6000 + 0.15*4000 + 4.25*1000) / 1e6
    assert cost_usd("muse-spark-1.3", 10_000, 4_000, 1_000) == pytest.approx(0.01235)
