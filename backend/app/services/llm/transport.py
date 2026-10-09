"""三个模型协议适配器共用的一次 HTTP 调用：截止时间、重试、熔断、计量与观测。

适配器只负责协议本身（地址、请求头、请求体、响应解析、用量换算）。这里的每一条
行为都曾在某个适配器里单独修过一次——截止时间约束单次超时、429 另有额度、额度耗尽
不重试——放在一处，修复不会只落到其中一个协议上。
"""

from __future__ import annotations

import time

import httpx

from backend.app.services.llm.call_log import log_call_failed
from backend.app.services.llm.call_log import log_call_succeeded
from backend.app.services.llm.debug_logging import log_llm_exception
from backend.app.services.llm.debug_logging import log_llm_request
from backend.app.services.llm.debug_logging import log_llm_response
from backend.app.services.llm.debug_logging import log_llm_retry_sleep
from backend.app.services.llm.errors import project_provider_error
from backend.app.services.llm.errors import raise_provider_call_error
from backend.app.services.llm.rate_limit import provider_circuit_key
from backend.app.services.llm.rate_limit import provider_request_slot
from backend.app.services.llm.retry import deadline_timeout
from backend.app.services.llm.retry import exponential_delay_seconds
from backend.app.services.llm.retry import retry_delay_seconds
from backend.app.services.llm.retry import retry_fits_before_deadline
from backend.app.services.llm.retry import retry_reason
from backend.app.services.llm_observability import observation


def langfuse_usage(usage):
    return {
        key: value
        for key, value in {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }.items()
        if value is not None
    }


def post_with_retry(
    scorer,
    *,
    url,
    headers,
    payload,
    provider_label,
    operation,
    model_parameters,
    configured_retries,
    usage_of,
    success_output=None,
    attempts_limit=None,
    timeout_seconds=None,
    rate_limit_retries=None,
    deadline=None,
):
    """Send one provider request with the shared retry/deadline/circuit policy.

    ``scorer`` supplies ``client``, ``model_name``, ``base_url``,
    ``timeout_seconds``, ``usage_meter`` and the optional connection snapshot.
    ``usage_of(data)`` maps a decoded success body onto the neutral usage
    fields; ``success_output(data)`` adds protocol-specific trace fields.
    """

    base_attempts = (
        max(1, configured_retries + 1) if attempts_limit is None else max(1, attempts_limit)
    )
    # 429 另有额度：超时与 5xx 只用 base_attempts（起草、归类传 1，避免把一次超时放大成
    # 多次长等待），限流则按 Retry-After 再等几次。默认不追加，评分路径行为不变。
    attempts = base_attempts + max(0, rate_limit_retries or 0)
    last_error = None
    with observation(
        "llm_generation",
        as_type="generation",
        input=payload,
        model=scorer.model_name,
        model_parameters=model_parameters,
        metadata={
            "gen_ai.provider.name": provider_label,
            "gen_ai.operation.name": operation,
            "server.address": scorer.base_url,
            "attempt_limit": attempts,
        },
    ) as generation:
        for attempt in range(attempts):
            # 调用方的截止时间（例如起草的总预算）同时约束每次调用的超时，
            # 否则单次调用可以越过预算，被平台强行终止。
            # 没有截止时间、也没显式超时时不传 timeout，沿用客户端默认值（与改动前一致）。
            attempt_timeout, expired = deadline_timeout(
                deadline,
                timeout_seconds if timeout_seconds is not None
                else (scorer.timeout_seconds if deadline is not None else None),
            )
            if expired:
                raise_provider_call_error(
                    provider_label, last_error or httpx.ReadTimeout("request deadline reached")
                )
            try:
                log_llm_request(provider_label, url, headers, payload, attempt, attempts)
                started = time.perf_counter()
                with observation(
                    "retry_attempt",
                    metadata={"attempt": attempt + 1, "attempt_limit": attempts},
                ):
                    connection_key = provider_circuit_key(
                        getattr(scorer, "_ai_connection_snapshot", None),
                        base_url=scorer.base_url,
                        model_name=scorer.model_name,
                    )
                    with provider_request_slot(
                        provider=provider_label,
                        connection_key=connection_key,
                    ) as slot:
                        response = scorer.client.post(
                            url, headers=headers, json=payload,
                            **({"timeout": attempt_timeout} if attempt_timeout is not None else {}),
                        )
                        slot.record_response(response)
                elapsed_ms = (time.perf_counter() - started) * 1000
                log_llm_response(provider_label, response, elapsed_ms, attempt, attempts)
                response.raise_for_status()
                data = response.json()
                log_call_succeeded(
                    provider_label, scorer.model_name, data,
                    elapsed_ms=elapsed_ms, attempt=attempt,
                )
                usage = usage_of(data)
                scorer.usage_meter.record_success(usage)
                generation.update(
                    output={
                        "provider_response_id": data.get("id"),
                        "status_code": getattr(response, "status_code", 200),
                        **(success_output(data) if success_output else {}),
                    },
                    usage_details=langfuse_usage(usage),
                    metadata={
                        "elapsed_ms": round(elapsed_ms, 2),
                        "attempts_used": attempt + 1,
                        "routed_model": data.get("model") if isinstance(data, dict) else None,
                        "upstream_provider": data.get("provider") if isinstance(data, dict) else None,
                    },
                )
                return response
            except httpx.HTTPStatusError as exc:
                last_error = exc
                scorer.usage_meter.record_failure()
                projected = project_provider_error(exc)
                generation.update(
                    level="ERROR",
                    status_message=projected.code,
                    metadata={"provider_error": projected.to_mapping()},
                )
                log_llm_exception(provider_label, exc, attempt, attempts)
                if projected.code == "rate_limited":
                    will_retry = projected.retryable and attempt < attempts - 1
                else:
                    will_retry = projected.retryable and attempt < base_attempts - 1
                delay_seconds = retry_delay_seconds(exc, attempt) if will_retry else 0.0
                will_retry = will_retry and retry_fits_before_deadline(deadline, delay_seconds)
                log_call_failed(
                    provider_label, scorer.model_name, projected,
                    attempt=attempt, attempts=attempts, will_retry=will_retry,
                )
                if not will_retry:
                    raise_provider_call_error(provider_label, exc)
                log_llm_retry_sleep(
                    provider_label, delay_seconds, attempt, attempts, retry_reason(exc),
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                scorer.usage_meter.record_failure()
                projected = project_provider_error(exc)
                generation.update(
                    level="ERROR",
                    status_message=projected.code,
                    metadata={"provider_error": projected.to_mapping()},
                )
                log_llm_exception(provider_label, exc, attempt, attempts)
                will_retry = attempt < base_attempts - 1
                delay_seconds = exponential_delay_seconds(attempt) if will_retry else 0.0
                will_retry = will_retry and retry_fits_before_deadline(deadline, delay_seconds)
                log_call_failed(
                    provider_label, scorer.model_name, projected,
                    attempt=attempt, attempts=attempts, will_retry=will_retry,
                )
                if not will_retry:
                    raise_provider_call_error(provider_label, exc)
                log_llm_retry_sleep(
                    provider_label, delay_seconds, attempt, attempts, retry_reason(exc),
                )
            time.sleep(delay_seconds)
    raise_provider_call_error(provider_label, last_error)


__all__ = ["langfuse_usage", "post_with_retry"]
