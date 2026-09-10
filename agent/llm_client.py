"""Unified LLM client supporting Anthropic Claude, OpenAI-compatible APIs, and internal endpoints."""

from __future__ import annotations

import json
import os
from typing import Any


class LLMClient:
    """Abstraction over multiple LLM providers.

    Supports:
    - Anthropic Claude API (ANTHROPIC_API_KEY)
    - OpenAI-compatible APIs (LLM_API_KEY + LLM_BASE_URL)
    - Any internal endpoint that speaks the OpenAI chat completions protocol

    Configuration via environment variables:
        LLM_PROVIDER=anthropic|openai|internal  (default: auto-detect)
        LLM_BASE_URL=https://your-internal-llm.company.com/v1
        LLM_API_KEY=your-key
        LLM_MODEL=model-name
        ANTHROPIC_API_KEY=sk-ant-...  (Anthropic-specific)
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        provider: str | None = None,
    ) -> None:
        self._provider = provider or os.environ.get("LLM_PROVIDER", "")
        self._base_url = base_url or os.environ.get("LLM_BASE_URL", "")
        self._model = model or os.environ.get("LLM_MODEL", "")
        self._client = None
        self._client_type = ""

        resolved_key = api_key or os.environ.get("LLM_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")

        if not resolved_key:
            return

        # Auto-detect provider
        if not self._provider:
            if os.environ.get("ANTHROPIC_API_KEY") and not self._base_url:
                self._provider = "anthropic"
            elif self._base_url:
                self._provider = "openai"
            elif resolved_key.startswith("sk-ant-"):
                self._provider = "anthropic"
            else:
                self._provider = "openai"

        if self._provider == "anthropic":
            self._init_anthropic(resolved_key)
        else:
            self._init_openai(resolved_key)

    def _init_anthropic(self, api_key: str) -> None:
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key)
            self._client_type = "anthropic"
            if not self._model:
                self._model = "claude-sonnet-4-6"
        except ImportError:
            pass

    def _init_openai(self, api_key: str) -> None:
        try:
            from openai import OpenAI
            kwargs: dict[str, Any] = {"api_key": api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = OpenAI(**kwargs)
            self._client_type = "openai"
            if not self._model:
                self._model = "gpt-4"
        except ImportError:
            # Fallback: use requests directly for OpenAI-compatible APIs
            if self._base_url:
                self._client = {"api_key": api_key, "base_url": self._base_url}
                self._client_type = "requests"
                if not self._model:
                    self._model = "default"

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def provider_name(self) -> str:
        if not self._client:
            return "none"
        return f"{self._provider} ({self._model})"

    def generate(self, prompt: str, system_prompt: str, max_tokens: int = 16000) -> str:
        if not self._client:
            return ""

        try:
            if self._client_type == "anthropic":
                return self._generate_anthropic(prompt, system_prompt, max_tokens)
            elif self._client_type == "openai":
                return self._generate_openai(prompt, system_prompt, max_tokens)
            elif self._client_type == "requests":
                return self._generate_requests(prompt, system_prompt, max_tokens)
        except Exception:
            return ""
        return ""

    def analyze_with_tool(
        self, prompt: str, system_prompt: str, tool_schema: dict, tool_name: str
    ) -> dict | None:
        if not self._client:
            return None

        try:
            if self._client_type == "anthropic":
                return self._analyze_anthropic(prompt, system_prompt, tool_schema, tool_name)
            else:
                return self._analyze_openai(prompt, system_prompt, tool_schema, tool_name)
        except Exception:
            return None

    def _generate_anthropic(self, prompt: str, system_prompt: str, max_tokens: int) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""

    def _generate_openai(self, prompt: str, system_prompt: str, max_tokens: int) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content or ""

    def _generate_requests(self, prompt: str, system_prompt: str, max_tokens: int) -> str:
        import urllib.request
        base_url = self._client["base_url"].rstrip("/")
        url = f"{base_url}/chat/completions"
        payload = json.dumps({
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._client['api_key']}",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]

    def _analyze_anthropic(
        self, prompt: str, system_prompt: str, tool_schema: dict, tool_name: str
    ) -> dict | None:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system_prompt,
            tools=[tool_schema],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input
        return None

    def _analyze_openai(
        self, prompt: str, system_prompt: str, tool_schema: dict, tool_name: str
    ) -> dict | None:
        # For OpenAI-compatible APIs, ask for JSON output
        combined_prompt = (
            f"{prompt}\n\n"
            f"Respond with a JSON object containing these fields: "
            f"{', '.join(tool_schema.get('input_schema', {}).get('required', []))}"
        )
        result = self.generate(combined_prompt, system_prompt + "\nRespond ONLY with valid JSON.", 4096)
        if result:
            # Extract JSON from response
            try:
                brace_start = result.find("{")
                if brace_start >= 0:
                    return json.loads(result[brace_start:])
            except json.JSONDecodeError:
                pass
        return None
