"""Provider-isolated, timeout-safe JSON assistance for Phase 4."""

from __future__ import annotations

import asyncio
import json
import os
import math
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional


Transport = Callable[
    [str, Dict[str, Any], Dict[str, str], float], Awaitable[Dict[str, Any]]
]


ANSWER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "canonical_intent": {"type": "string"},
        "answer": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "needs_user_input": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": [
        "canonical_intent", "answer", "confidence", "needs_user_input", "notes"
    ],
}


@dataclass(frozen=True)
class LLMResult:
    ok: bool = False
    canonical_intent: str = ""
    answer: str = ""
    confidence: float = 0.0
    needs_user_input: bool = True
    notes: str = ""
    data: Optional[Dict[str, Any]] = None
    error: str = ""


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool = False
    provider: str = "none"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout_seconds: float = 10.0
    minimum_confidence: float = 0.85

    @classmethod
    def from_profile(cls, profile: Dict[str, Any]) -> "LLMSettings":
        config = profile.get("ai_settings", {})
        provider = os.getenv("LLM_PROVIDER", str(config.get("provider", "none"))).strip().lower()
        env_enabled = bool(os.getenv("LLM_PROVIDER")) and provider not in ("", "none", "off")
        enabled = (_as_bool(os.environ["LLM_ENABLED"]) if "LLM_ENABLED" in os.environ
                   else _as_bool(config.get("enabled", False)) or env_enabled)
        base_url = os.getenv("LLM_BASE_URL", str(config.get("base_url", ""))).strip()
        if provider == "lmstudio" and not base_url:
            base_url = "http://localhost:1234/v1"
        key_env = str(config.get("api_key_env", "OPENAI_API_KEY") or "OPENAI_API_KEY")
        api_key = os.getenv("LLM_API_KEY", os.getenv(key_env, "")).strip()
        try:
            timeout = max(0.1, float(os.getenv("LLM_TIMEOUT_SECONDS", config.get("timeout_seconds", 10))))
        except (TypeError, ValueError):
            timeout = 10.0
        try:
            confidence = max(0.0, min(1.0, float(config.get("minimum_confidence", 0.85))))
        except (TypeError, ValueError):
            confidence = 0.85
        return cls(
            enabled=enabled,
            provider=provider,
            base_url=base_url,
            model=os.getenv("LLM_MODEL", str(config.get("model", ""))).strip(),
            api_key=api_key,
            timeout_seconds=timeout,
            minimum_confidence=confidence,
        )


class LLMProvider(ABC):
    """Abstract bounded semantic provider; it never receives a browser page."""

    provider_name = "abstract"

    def __init__(self, settings: LLMSettings, transport: Optional[Transport] = None):
        self.settings = settings
        self.transport = transport or _post_json

    @property
    def available(self) -> bool:
        return bool(
            self.settings.enabled
            and self.settings.model
            and self.settings.base_url
        )

    async def interpret_question(
        self,
        question: str,
        verified_facts: Dict[str, Any],
        options: Optional[List[str]] = None,
    ) -> LLMResult:
        if not self.available:
            return LLMResult(error="provider_not_configured")
        prompt = {
            "task": "Map the question to a canonical intent and answer only from verified facts.",
            "question": question,
            "options": options or [],
            "verified_candidate_facts": verified_facts,
            "rules": [
                "Never invent a factual candidate detail.",
                "If verified facts are insufficient, set needs_user_input true and answer empty.",
                "If options are supplied, use the exact matching option text.",
            ],
        }
        result = await self.generate_json(prompt, ANSWER_SCHEMA, "application_answer")
        if not result.ok or not result.data:
            return result
        data = result.data
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        answer = str(data.get("answer", "") or "").strip()
        needs_input = bool(data.get("needs_user_input", False)) or not answer
        return LLMResult(
            ok=not needs_input,
            canonical_intent=str(data.get("canonical_intent", "") or "").strip(),
            answer=answer,
            confidence=confidence,
            needs_user_input=needs_input,
            notes=str(data.get("notes", "") or "").strip(),
            data=data,
        )

    async def generate_json(
        self, payload: Dict[str, Any], schema: Dict[str, Any], schema_name: str
    ) -> LLMResult:
        if not self.available:
            return LLMResult(error="provider_not_configured")
        try:
            response = await asyncio.wait_for(
                self.transport(
                    self.endpoint,
                    self.build_request(payload, schema, schema_name),
                    self.headers,
                    self.settings.timeout_seconds,
                ),
                timeout=self.settings.timeout_seconds + 0.25,
            )
            raw_text = self.extract_text(response)
            data = _parse_json_text(raw_text)
            if not isinstance(data, dict):
                return LLMResult(error="invalid_json")
            if not _valid_schema(data, schema):
                return LLMResult(error="invalid_schema")
            return LLMResult(ok=True, needs_user_input=False, data=data)
        except (asyncio.TimeoutError, TimeoutError):
            return LLMResult(error="timeout")
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, urllib.error.URLError) as exc:
            return LLMResult(error=f"{type(exc).__name__}: {exc}")

    @property
    @abstractmethod
    def endpoint(self) -> str: ...

    @property
    def headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return headers

    @abstractmethod
    def build_request(
        self, payload: Dict[str, Any], schema: Dict[str, Any], schema_name: str
    ) -> Dict[str, Any]: ...

    @abstractmethod
    def extract_text(self, response: Dict[str, Any]) -> str: ...


class NullLLMProvider(LLMProvider):
    provider_name = "none"

    @property
    def available(self) -> bool:
        return False

    @property
    def endpoint(self) -> str:
        return ""

    def build_request(self, payload, schema, schema_name):
        return {}

    def extract_text(self, response):
        return ""


class OpenAICompatibleProvider(LLMProvider):
    provider_name = "openai-compatible"

    @property
    def endpoint(self) -> str:
        return self.settings.base_url.rstrip("/") + "/chat/completions"

    def build_request(self, payload, schema, schema_name):
        return {
            "model": self.settings.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": "Return one JSON object. Use only supplied verified facts.",
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }

    def extract_text(self, response):
        return str(response["choices"][0]["message"]["content"])


class LMStudioProvider(OpenAICompatibleProvider):
    provider_name = "lmstudio"


class GeminiProvider(OpenAICompatibleProvider):
    """Gemini through a user-configured OpenAI-compatible endpoint."""

    provider_name = "gemini"


class OpenAIProvider(LLMProvider):
    provider_name = "openai"

    @property
    def available(self) -> bool:
        return super().available and bool(self.settings.api_key)

    @property
    def endpoint(self) -> str:
        return self.settings.base_url.rstrip("/") + "/responses"

    def build_request(self, payload, schema, schema_name):
        return {
            "model": self.settings.model,
            "store": False,
            "instructions": "Return structured JSON. Use only supplied verified candidate facts.",
            "input": json.dumps(payload, ensure_ascii=False),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }

    def extract_text(self, response):
        if isinstance(response.get("output_text"), str):
            return response["output_text"]
        for item in response.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return str(content.get("text", ""))
        raise ValueError("Response contained no output text")


def create_llm_provider(
    profile: Dict[str, Any], transport: Optional[Transport] = None
) -> LLMProvider:
    settings = LLMSettings.from_profile(profile)
    providers = {
        "lmstudio": LMStudioProvider,
        "openai": OpenAIProvider,
        "gemini": GeminiProvider,
        "openai-compatible": OpenAICompatibleProvider,
        "openai_compatible": OpenAICompatibleProvider,
    }
    provider_class = providers.get(settings.provider)
    if not settings.enabled or provider_class is None:
        return NullLLMProvider(settings, transport)
    if settings.provider == "openai" and not settings.base_url:
        settings = LLMSettings(**{**settings.__dict__, "base_url": "https://api.openai.com/v1"})
    return provider_class(settings, transport)


async def _post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    timeout: float,
) -> Dict[str, Any]:
    def send() -> Dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    return await asyncio.to_thread(send)


def _valid_schema(value: Any, schema: Dict[str, Any]) -> bool:
    """Validate the small JSON schema subset used by answer/match responses."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict) or any(key not in value for key in schema.get("required", [])):
            return False
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(_valid_schema(item, properties[key]) for key, item in value.items() if key in properties)
    if kind == "array":
        return isinstance(value, list) and all(_valid_schema(item, schema.get("items", {})) for item in value)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return False
        if kind == "integer" and int(value) != value:
            return False
        return schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf)
    return True


def _parse_json_text(text: str) -> Any:
    clean = str(text or "").strip()
    if clean.startswith("```"):
        clean = clean.removeprefix("```json").removeprefix("```")
        clean = clean.removesuffix("```").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        start, end = clean.find("{"), clean.rfind("}")
        if start >= 0 and end > start:
            return json.loads(clean[start : end + 1])
        raise ValueError("Provider did not return valid JSON")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
