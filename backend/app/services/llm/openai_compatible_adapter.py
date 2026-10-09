import json
from urllib.parse import urlparse

import httpx

from backend.app.core.config import settings
from backend.app.services.llm.base import LLMScorer
from backend.app.services.llm.base import validated_envelope_provider
from backend.app.services.llm.core_adapter import validated_core_envelope_provider
from backend.app.services.llm.core_view import build_core_group_request
from backend.app.services.llm.core_view import build_core_request
from backend.app.services.llm.core_view import compression_summary
from backend.app.services.llm.core_view import decode_core_group_response
from backend.app.services.llm.core_view import decode_core_response
from backend.app.services.llm.core_view import preflight_core_request
from backend.app.services.llm.errors import ProviderJSONOutputError
from backend.app.services.llm.legacy_prompts import chat_envelope_instructions as _envelope_instructions
from backend.app.services.llm.legacy_prompts import chat_input_payload as _input_payload
from backend.app.services.llm.legacy_prompts import chat_instructions as _instructions
from backend.app.services.llm.legacy_prompts import strip_json_fence as _strip_json_fence
from backend.app.services.llm.transport import post_with_retry
from backend.app.services.llm.usage import UsageMeter


class OpenAICompatibleChatScorer(LLMScorer):
    provider = "openai_compatible"
    model_version = "chat-completions"

    def __init__(self, api_key=None, base_url=None, model_name=None, provider_name=None, client=None, timeout_seconds=None, max_tokens=None, temperature=None, top_p=None, response_format_json=None, thinking_type=None, service_tier=None):
        self.api_key = api_key or settings.OPENAI_COMPATIBLE_API_KEY
        if not self.api_key:
            raise ValueError("OPENAI_COMPATIBLE_API_KEY is required when LLM_PROVIDER=openai_compatible")
        self.base_url = base_url or settings.OPENAI_COMPATIBLE_BASE_URL
        if not self.base_url:
            raise ValueError("OPENAI_COMPATIBLE_BASE_URL is required when LLM_PROVIDER=openai_compatible")
        self.base_url = self.base_url.rstrip("/")
        self.model_name = model_name or settings.OPENAI_COMPATIBLE_MODEL
        self.provider_name = provider_name or settings.OPENAI_COMPATIBLE_PROVIDER_NAME
        self.timeout_seconds_explicit = timeout_seconds is not None
        self.timeout_seconds = float(timeout_seconds or settings.OPENAI_COMPATIBLE_TIMEOUT_SECONDS)
        self.max_tokens_explicit = max_tokens is not None
        self.max_tokens = int(max_tokens or settings.OPENAI_COMPATIBLE_MAX_TOKENS)
        self.temperature = float(temperature if temperature is not None else settings.OPENAI_COMPATIBLE_TEMPERATURE)
        # Chat endpoints disagree on the default top_p (Zhipu/Qwen are < 1),
        # so the frozen value is always sent; a connection may override it
        # (Bailian kimi-k3 rejects 1.0 but accepts 0.95).
        self.top_p = float(top_p if top_p is not None else 1)
        self.response_format_json = settings.OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON if response_format_json is None else bool(response_format_json)
        self.thinking_type = settings.OPENAI_COMPATIBLE_THINKING_TYPE if thinking_type is None else thinking_type
        self.service_tier = (
            settings.OPENAI_COMPATIBLE_SERVICE_TIER
            if service_tier is None
            else service_tier
        )
        if self.service_tier not in {None, "auto", "on_demand", "flex", "performance"}:
            raise ValueError("unsupported OpenAI-compatible service_tier")
        self._owns_client = client is None
        self.usage_meter = UsageMeter()
        self.client = client or httpx.Client(timeout=self.timeout_seconds)

    def close(self):
        if self._owns_client and self.client is not None:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def score_criterion(self, paper, criterion, evidence_candidates, structure_checks, anchors=None):
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": _instructions(criterion)},
                {"role": "user", "content": _input_payload(paper, criterion, evidence_candidates, structure_checks, anchors)},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        thinking_type = (self.thinking_type or "").strip()
        if thinking_type:
            payload["thinking"] = {"type": thinking_type}
        if self.response_format_json:
            payload["response_format"] = {"type": "json_object"}
        if self.service_tier:
            payload["service_tier"] = self.service_tier

        response = self._post_with_retry(payload)
        response.raise_for_status()
        data = response.json()
        output = _parse_chat_json_output(data)
        output.setdefault("criterion_id", criterion.id)
        output.setdefault("criterion_name", criterion.name)
        output.setdefault("max_score", float(criterion.max_score))
        output["provider"] = self.provider_name
        output["provider_response_id"] = data.get("id")
        output["usage"] = _usage_from_chat(data)
        return output

    def score_envelope(self, envelope):
        """Send the provider-ready envelope without reconstructing its inputs."""

        envelope, provider = validated_envelope_provider(self, envelope)
        envelope_payload = envelope.to_mapping()
        criterion = envelope_payload["criterion"]
        if provider["response_schema"] != "criterion-score-v2":
            raise ValueError("unsupported PromptEnvelope response_schema")
        if provider["response_format"] not in {"none", "json_object"}:
            raise ValueError("OpenAI-compatible PromptEnvelope response_format is unsupported")
        sampling = provider["sampling"]
        payload = {
            "model": provider["model"],
            "messages": [
                {
                    "role": "system",
                    "content": _envelope_instructions(criterion["scoring_mode"]),
                },
                {
                    "role": "user",
                    "content": json.dumps(envelope_payload, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "temperature": float(sampling["temperature"]),
            "top_p": float(sampling["top_p"]),
            "max_tokens": sampling["max_tokens"],
        }
        if sampling["seed"] is not None:
            payload["seed"] = sampling["seed"]
        thinking_type = provider["thinking"]["type"]
        if thinking_type:
            payload["thinking"] = {"type": thinking_type}
        if provider["response_format"] == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if self.service_tier:
            payload["service_tier"] = self.service_tier

        response = self._post_with_retry(payload)
        response.raise_for_status()
        data = response.json()
        output = _parse_chat_json_output(data)
        output.setdefault("criterion_id", criterion["code"])
        output.setdefault("criterion_name", criterion["name"])
        output.setdefault("max_score", float(criterion["max_score"]))
        output["provider"] = self.provider_name
        output["provider_response_id"] = data.get("id")
        output["usage"] = _usage_from_chat(data)
        return output

    def _validated_core(self, envelope):
        envelope, provider = validated_core_envelope_provider(self, envelope)
        if provider["response_format"] not in {"none", "json_object"}:
            raise ValueError(
                "OpenAI-compatible PromptEnvelopeV3 response_format is unsupported"
            )
        return envelope, provider

    def _post_core_request(self, envelope, provider, request):
        """Send exactly the provider view (never the identity envelope)."""

        preflight_core_request(envelope, request)
        sampling = provider["sampling"]
        payload = {
            "model": provider["model"],
            "messages": [
                # The system message is identical for every rule of a kind, so
                # it stays a cacheable prefix; per-rule content is in the view.
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": float(sampling["temperature"]),
            "top_p": float(sampling["top_p"]),
            "max_tokens": sampling["max_tokens"],
        }
        if sampling["seed"] is not None:
            payload["seed"] = sampling["seed"]
        thinking_type = provider["thinking"]["type"]
        if thinking_type:
            payload["thinking"] = {"type": thinking_type}
        if provider["response_format"] == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if self.service_tier:
            payload["service_tier"] = self.service_tier

        self.last_view_summary = compression_summary(request)
        response = self._post_with_retry(payload)
        response.raise_for_status()
        return _parse_chat_json_output(response.json())

    def score_core_envelope(self, *, envelope):
        """Score one immutable PromptEnvelopeV3/V4 semantic rule via its view."""

        envelope, provider = self._validated_core(envelope)
        request = build_core_request(envelope)
        raw = self._post_core_request(envelope, provider, request)
        return decode_core_response(envelope, raw, request)

    def score_core_group(self, *, envelopes, group_code):
        """Judge every tier of one mutex group in a single request."""

        validated = [self._validated_core(envelope) for envelope in envelopes]
        envelopes = [envelope for envelope, _provider in validated]
        request = build_core_group_request(envelopes, group_code=group_code)
        raw = self._post_core_request(envelopes[0], validated[0][1], request)
        return decode_core_group_response(envelopes, raw, request)

    def complete_json(self, instructions, payload, *, response_schema=None, default_max_tokens=None, attempts_limit=None, default_timeout_seconds=None, rate_limit_retries=None, deadline=None):
        request_options = {}
        if attempts_limit is not None:
            request_options["attempts_limit"] = attempts_limit
        if rate_limit_retries:
            request_options["rate_limit_retries"] = rate_limit_retries
        if deadline is not None:
            request_options["deadline"] = deadline
        if default_timeout_seconds is not None and not self.timeout_seconds_explicit:
            request_options["timeout_seconds"] = max(self.timeout_seconds, default_timeout_seconds)
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens if self.max_tokens_explicit or default_max_tokens is None else max(self.max_tokens, default_max_tokens),
        }
        thinking_type = (self.thinking_type or "").strip()
        if thinking_type:
            body["thinking"] = {"type": thinking_type}
        if self.response_format_json:
            body["response_format"] = {"type": "json_object"}
        if response_schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "rubric_rule_draft", "strict": True, "schema": response_schema,
            }}
        if self.service_tier:
            body["service_tier"] = self.service_tier
        if response_schema is not None:
            # ``reasoning`` is an OpenRouter extension.  Other OpenAI-compatible
            # providers (including Google AI Studio) may reject the same field,
            # even though they support strict ``json_schema`` output.
            if urlparse(self.base_url).hostname == "openrouter.ai" and (
                not thinking_type or thinking_type == "disabled"
            ):
                body["reasoning"] = {"enabled": False}
            # The drafting layer already permits one schema repair. Avoid multiplying
            # that by transport retries inside a synchronous serverless request.
            response = self._post_with_retry(body, **{**request_options, "attempts_limit": 1})
        else:
            response = self._post_with_retry(body, **request_options)
        response.raise_for_status()
        return _parse_chat_json_output(response.json())

    def _post_with_retry(self, payload, *, attempts_limit=None, timeout_seconds=None, rate_limit_retries=None, deadline=None):
        return post_with_retry(
            self,
            url="%s/chat/completions" % self.base_url,
            headers={
                "Authorization": "Bearer %s" % self.api_key,
                "Content-Type": "application/json",
            },
            payload=payload,
            provider_label=self.provider_name,
            operation="chat",
            model_parameters={
                "temperature": payload.get("temperature"),
                "top_p": payload.get("top_p"),
                "max_tokens": payload.get("max_tokens"),
            },
            configured_retries=settings.OPENAI_COMPATIBLE_MAX_RETRIES,
            usage_of=_usage_from_chat,
            success_output=lambda data: {"service_tier": data.get("service_tier")},
            attempts_limit=attempts_limit,
            timeout_seconds=timeout_seconds,
            rate_limit_retries=rate_limit_retries,
            deadline=deadline,
        )


def _usage_from_chat(data):
    usage = data.get("usage") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


class ChatJSONOutputError(ProviderJSONOutputError):
    """Safe metadata only; never include provider content in this exception."""

    def __init__(self, reason):
        super().__init__(reason, "OpenAI-compatible JSON output: " + reason)


def _parse_chat_json_output(data):
    if not isinstance(data, dict):
        raise ChatJSONOutputError("invalid_envelope")
    if data.get("error"):
        raise ChatJSONOutputError("error_envelope")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ChatJSONOutputError("missing_choices")
    choice = choices[0]
    truncated = choice.get("finish_reason") == "length"
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ChatJSONOutputError("missing_message")
    text = _message_content_to_text(message.get("content"))
    if not text.strip():
        raise ChatJSONOutputError("output_truncated" if truncated else "empty_content")
    try:
        return json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as exc:
        raise ChatJSONOutputError("output_truncated" if truncated else "invalid_json") from exc


def _message_content_to_text(content):
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content or "")


def _empty_content_detail(choice):
    message = choice.get("message") or {}
    reasoning = str(message.get("reasoning_content") or "")
    return json.dumps(
        {
            "finish_reason": choice.get("finish_reason"),
            "choice_keys": sorted(choice.keys()),
            "message_keys": sorted(message.keys()),
            "reasoning_content_chars": len(reasoning),
            "diagnosis": (
                "模型返回了 reasoning_content 但没有最终 content，通常是 thinking 模式消耗了输出预算。"
                if reasoning
                else "模型响应没有最终 content。请检查模型名、thinking 参数、max_tokens 和提示词。"
            ),
        },
        ensure_ascii=False,
    )
