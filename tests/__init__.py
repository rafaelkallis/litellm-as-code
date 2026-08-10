"""Fake API client attached to a LiteLLMClient (mirrors package layout)."""

from __future__ import annotations

from litellm_as_code.api import LiteLLMClient

from .fakes import FakeLiteLLM  # noqa: F401  (re-export for convenience)


def make_fake_client() -> tuple[LiteLLMClient, FakeLiteLLM]:
    fake = FakeLiteLLM()
    client = LiteLLMClient("http://fake:4000", "test-admin-key")
    fake.attach(client)
    return client, fake
