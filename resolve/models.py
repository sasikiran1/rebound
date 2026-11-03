"""Model clients for Resolve: Meta's Model API (Muse Spark) or a local Ollama model, behind one `Model`.

Names starting with `muse-` use the Meta API (needs MODEL_API_KEY; billed). Anything else is an Ollama model reached
through the Windows relay (harness/ollama.py); it is free and only works where that relay runs.
"""

from __future__ import annotations

from pathlib import Path

from resolve.interpret import Model

OLLAMA_OPTIONS = {"temperature": 0, "seed": 41, "num_ctx": 16384}


class MetaClient:
    """`client.responses.create` over harness.provider.Provider (its retry table applies)."""

    def __init__(self):
        from harness.provider import Provider, make_client
        self.provider = Provider(make_client())
        self.responses = self

    async def create(self, **kwargs):
        return await self.provider.create(**kwargs)

    async def close(self):
        await self.provider.client.close()


def make_model(name: str, raw_dir: Path | None = None, max_calls: int | None = None,
               max_cost: float | None = None) -> Model:
    if name.startswith("muse-"):
        return Model(MetaClient(), name, max_output_tokens=4096, reasoning_effort="low", max_calls=max_calls,
                     max_cost_usd=max_cost)
    from harness.ollama import OllamaClient
    think = None if name.startswith(("qwen2.5", "llama", "hf.co")) else False  # None: no thinking mode to turn off
    return Model(OllamaClient(OLLAMA_OPTIONS, think=think, raw_dir=raw_dir), name, max_output_tokens=2048,
                 reasoning_effort=None, max_calls=max_calls, max_cost_usd=max_cost)
