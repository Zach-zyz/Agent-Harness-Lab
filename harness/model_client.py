"""Credential-safe hosted API and deterministic replay clients for HW2."""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import signal
import threading
from functools import wraps
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .budget import Budget, BudgetExceeded

DEFAULT_MODEL_ID = "openai/gpt-oss-20b"
DEFAULT_MODEL_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL_API_KEY = ""


def model_environment(name: str, default: str) -> str:
    return os.environ.get(f"HW2_MODEL_{name}", os.environ.get(f"COURSE_MODEL_{name}", default))


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_model_id(model_id: str) -> None:
    if not model_id or len(model_id) > 256 or any(c.isspace() or ord(c) < 33 for c in model_id):
        raise ValueError("HW2_MODEL_ID must be a nonempty model identifier without whitespace")


def validate_api_key(api_key: str) -> None:
    if not api_key or not api_key.isascii() or any(not 33 <= ord(c) <= 126 for c in api_key):
        raise ValueError("Set HW2_MODEL_API_KEY to your own provider key (nonempty printable ASCII)")


@dataclass(frozen=True)
class APIModelConfig:
    base_url: str
    api_key: str = field(repr=False)
    model_id: str
    action_mode: str = "native"
    reasoning_effort: str = ""


def resolve_model_config() -> APIModelConfig:
    base_url = model_environment("BASE_URL", DEFAULT_MODEL_BASE_URL).rstrip("/")
    try:
        url = urlsplit(base_url)
        valid = (
            (url.scheme == "https" or (url.scheme == "http" and is_loopback_host(url.hostname or "")))
            and bool(url.hostname) and url.username is None and url.password is None
            and not url.query and not url.fragment
            and (url.port is None or 1 <= url.port <= 65535)
            and not any(c.isspace() or ord(c) < 32 for c in base_url)
            and "\\" not in base_url
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("HW2_MODEL_BASE_URL must be an HTTPS API base URL without credentials/query/fragment; HTTP is permitted only on loopback for local testing")
    model_id = model_environment("ID", DEFAULT_MODEL_ID)
    validate_model_id(model_id)
    api_key = model_environment("API_KEY", DEFAULT_MODEL_API_KEY)
    validate_api_key(api_key)
    mode = model_environment("ACTION_MODE", "native")
    if mode not in {"native", "json"}:
        raise ValueError("HW2_MODEL_ACTION_MODE must be native or json")
    effort = model_environment("REASONING_EFFORT", "low" if base_url == DEFAULT_MODEL_BASE_URL else "")
    if effort not in {"", "low", "medium", "high"}:
        raise ValueError("HW2_MODEL_REASONING_EFFORT must be empty, low, medium or high")
    return APIModelConfig(base_url, api_key, model_id, mode, effort)


@dataclass(frozen=True)
class ModelResponse:
    content: str
    input_tokens: int
    output_tokens: int
    usage_charged: bool = False


class ModelClient(Protocol):
    def complete(self, messages: list[dict[str, str]], seed: int) -> ModelResponse: ...


class ReplayModelClient:
    def __init__(self, replay_path: str | Path) -> None:
        payload = json.loads(Path(replay_path).read_text(encoding="utf-8"))
        self.responses = payload["responses"]
        self.index = 0

    def complete(self, messages: list[dict[str, str]], seed: int) -> ModelResponse:
        del messages, seed
        if self.index >= len(self.responses):
            raise RuntimeError("Replay exhausted before the agent finished")
        item = self.responses[self.index]
        self.index += 1
        return ModelResponse(
            content=item["content"],
            input_tokens=item.get("input_tokens", 100),
            output_tokens=item.get("output_tokens", 50),
        )



MAX_RETRIES = 2
MAX_RETRY_WAIT_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 200.0
REQUEST_GAP_SECONDS = 2.0  # <=30 RPM, below the owner's verified 40 RPM limit.
MAX_RESPONSE_BYTES = 4_000_000
ACTION_PROTOCOL = "native_tool_calls_or_final_json_v1"
TOOL_PARAMETERS = {
    "list_files": ({"path": {"type": "string"}}, []),
    "read_file": ({
        "path": {"type": "string"}, "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
    }, ["path"]),
    "search": ({"query": {"type": "string"}, "path": {"type": "string"}}, ["query"]),
    "write_file": ({
        "path": {"type": "string"}, "content": {"type": "string"},
    }, ["path", "content"]),
    "run_tests": ({"command": {"type": "string"}}, []),
    "finish": ({"summary": {"type": "string"}}, ["summary"]),
}
NATIVE_TOOLS = [
    {"type": "function", "function": {
        "name": name, "description": f"Choose the {name} action for the repair controller.",
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}
    for name, (properties, required) in TOOL_PARAMETERS.items()
]


class ProviderError(RuntimeError):
    """Credential-free provider error with API evidence retained on disk."""


def sanitize(value: Any, secret: str = "") -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return "[NONFINITE]"
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
                       "Bearer [REDACTED]", value)
        value = re.sub(r"\bnvapi-[A-Za-z0-9_-]+", "[REDACTED]", value)
        return value
    if isinstance(value, dict):
        return {
            sanitize(str(key), secret): (
                "[REDACTED]" if re.search(
                    r"(?i)(authorization|api[_-]?key|password|access[_-]?token|secret)", str(key)
                ) else sanitize(item, secret)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item, secret) for item in value]
    return value


def write_json(path: Path, value: Any, secret: str = "") -> None:
    path.write_text(json.dumps(sanitize(value, secret), indent=2, sort_keys=True,
                               allow_nan=False) + "\n", encoding="utf-8")


def _count(usage: dict, name: str) -> int:
    value = usage.get(name)
    if type(value) is not int or value < 0:
        raise ProviderError(f"invalid_usage_{name}")
    return value


def account_usage(usage: Any) -> dict:
    """Preserve total_tokens; nested OpenAI reasoning counts are completion subsets.

    A top-level reasoning count has ambiguous inclusion semantics. Charge it
    additionally unless total_tokens already covers it; disclose the adjustment.
    Never derive output tokens from final-answer text length.
    """
    if not isinstance(usage, dict):
        raise ProviderError("missing_usage")
    prompt = _count(usage, "prompt_tokens")
    completion = _count(usage, "completion_tokens")
    total = _count(usage, "total_tokens")
    if total < prompt + completion:
        raise ProviderError("usage_total_smaller_than_prompt_plus_completion")
    details = usage.get("completion_tokens_details")
    if details is not None and not isinstance(details, dict):
        raise ProviderError("invalid_completion_tokens_details")
    nested = _count(details, "reasoning_tokens") if (
        isinstance(details, dict) and "reasoning_tokens" in details
    ) else None
    top = _count(usage, "reasoning_tokens") if "reasoning_tokens" in usage else None
    reasoning = max(x for x in (nested, top, 0) if x is not None)
    accounted = max(total, prompt + completion)
    notes = []
    if top is not None or (nested is not None and nested > completion):
        accounted = max(accounted, prompt + completion + reasoning)
        notes.append("reasoning_inclusion_ambiguous_conservative_addition")
    elif nested is not None:
        notes.append("nested_reasoning_is_completion_subset")
    if total > prompt + completion:
        notes.append("total_tokens_excess_charged_as_output")
    return {
        "input_tokens": prompt,
        "output_tokens": accounted - prompt,
        "charged_tokens": accounted,
        "reported_total_tokens": total,
        "reported_reasoning_tokens": reasoning if nested is not None or top is not None else None,
        "accounting_notes": notes,
    }


def retry_wait(headers: dict[str, str], retry_index: int) -> tuple[float, str]:
    """Honor standard Retry-After delta seconds or HTTP-date, never retry early."""
    lower = {k.lower(): v for k, v in headers.items()}
    value = lower.get("retry-after")
    if value is not None:
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                return seconds, "retry-after"
        except (ValueError, TypeError):
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is not None:
                    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds()), "retry-after"
            except (ValueError, TypeError, OverflowError):
                pass
    return float(2 ** (retry_index + 1)), "bounded-backoff"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _RequestDeadlineExpired(RuntimeError):
    """Must not inherit OSError: socket address retries catch that exception."""


def _total_request_deadline(method):
    """Bound DNS/connect/TLS/headers/body together for the serial main-thread client."""
    @wraps(method)
    def wrapped(self, body, key, timeout):
        if threading.current_thread() is not threading.main_thread():
            raise ProviderError("HTTP client must run serially in the main thread")
        if not hasattr(signal, "setitimer") or timeout <= 0:
            raise ProviderError("total_request_deadline_unavailable")
        if signal.getitimer(signal.ITIMER_REAL)[0] > 0:
            raise ProviderError("existing_process_alarm_conflicts_with_request_deadline")
        previous = signal.getsignal(signal.SIGALRM)
        def expired(signum, frame):
            raise _RequestDeadlineExpired("total_request_deadline_exceeded")
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            return method(self, body, key, timeout)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    return wrapped


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("nonfinite_json_number")
    return value


def _reject_constant(text):
    raise ValueError("nonfinite_json_number")


class HTTPTransport:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        # Do not forward credentials via environment-selected HTTP proxies.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    @_total_request_deadline
    def post(self, body: bytes, key: str, timeout: float) -> tuple[int, dict, Any]:
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=body, method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )
        deadline = time.monotonic() + timeout
        try:
            try:
                response = self.opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.code
                headers = {"retry-after": response.headers["Retry-After"]} if (
                    response.headers.get("Retry-After") is not None
                ) else {}
                chunks = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    # Bound each read by the remaining request deadline.
                    sock = getattr(getattr(response.fp, "raw", None), "_sock", None)
                    if sock is not None:
                        sock.settimeout(remaining)
                    chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        raise ProviderError("response_exceeds_size_limit")
                try:
                    payload = json.loads(chunks, parse_constant=lambda _: None)
                except (ValueError, UnicodeError):
                    payload = None  # Error bodies and exception text never enter logs.
                return status, headers, payload
        except Exception:
            # urllib errors may contain request details. Do not expose them.
            raise ProviderError("transport_failed_or_response_unreadable") from None


class OpenAICompatibleModelClient:
    def __init__(
        self, *, budget: Budget, output_dir: Path, config: APIModelConfig | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.2, top_p: float = 1.0, max_output_tokens: int = 4096,
        transport=None, sleep=time.sleep, clock=time.monotonic,
    ) -> None:
        config = config or resolve_model_config()
        key = config.api_key
        if not key or not key.isascii() or any(not 33 <= ord(c) <= 126 for c in key):
            raise ProviderError("invalid_key_format")
        if not 0 <= temperature <= 1 or not 0 < top_p <= 1:
            raise ProviderError("temperature_must_be_0_to_1_and_top_p_0_to_1")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 4096:
            raise ProviderError("max_tokens_must_be_1_to_4096")
        self.config = config
        self.base_url = config.base_url
        self.model = config.model_id
        self._key = key
        self.tools = NATIVE_TOOLS if tools is None else tools
        try:
            self.tool_names = {t["function"]["name"] for t in self.tools}
            if not self.tool_names or len(self.tool_names) != len(self.tools):
                raise ValueError
            if any(not isinstance(n, str) or not n for n in self.tool_names):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("tools must contain distinct named function schemas") from None
        self.budget = budget
        self.output_dir = output_dir
        self.temperature, self.top_p = temperature, top_p
        self.max_output_tokens = max_output_tokens
        self.transport = transport if transport is not None else HTTPTransport(config.base_url)
        self.sleep, self.clock = sleep, clock
        self.stats = {
            "logical_calls": 0, "http_attempts": 0, "retries": 0,
            "http_429": 0, "http_5xx": 0,
            "request_seconds": 0.0, "provider_wait_seconds": 0.0,
            "pacing_wait_seconds": 0.0, "provider_requested_wait_seconds": 0.0,
            "reported_total_tokens": 0, "reported_reasoning_tokens": 0,
            "reasoning_usage_reported_attempts": 0,
            "unknown_usage_attempts": 0, "reserved_unknown_tokens": 0,
            "usage_accounting_complete": True, "returned_models": [],
        }

    def _record(self, event: dict) -> None:
        with (self.output_dir / "api_attempts.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sanitize(event, self._key), sort_keys=True,
                                    allow_nan=False) + "\n")
        write_json(self.output_dir / "provider_metrics.json", self.stats, self._key)

    def _wait(self, seconds: float, field: str) -> None:
        self.budget.check()
        if seconds >= self.budget.max_active_seconds - self.budget.active_seconds:
            raise BudgetExceeded("provider wait would exceed the remaining episode budget")
        start = self.clock()
        try:
            self.sleep(seconds)
        finally:
            self.stats[field] += max(0.0, self.clock() - start)
        self.budget.check()

    def _usage(self, payload: Any, event: dict, reservation: int) -> tuple[int, int]:
        usage = payload.get("usage") if isinstance(payload, dict) else None
        # Exact JSON usage object, sanitized recursively, retained even if invalid.
        event["usage_raw"] = usage
        try:
            accounting = account_usage(usage)
        except ProviderError as error:
            # Even malformed usage can contain a large, valid reported count.
            # Retain that lower bound instead of charging only the reservation.
            if isinstance(usage, dict):
                known = {k: v for k, v in usage.items() if type(v) is int and v >= 0}
                details = usage.get("completion_tokens_details")
                nested_reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
                reasoning = max(
                    known.get("reasoning_tokens", 0),
                    nested_reasoning if type(nested_reasoning) is int and nested_reasoning >= 0 else 0,
                )
                reservation = max(
                    reservation, known.get("total_tokens", 0),
                    known.get("prompt_tokens", 0) + known.get("completion_tokens", 0)
                    + reasoning,
                )
                self.stats["reported_total_tokens"] += known.get("total_tokens", 0)
                if "reasoning_tokens" in known or type(nested_reasoning) is int:
                    self.stats["reported_reasoning_tokens"] += reasoning
                    self.stats["reasoning_usage_reported_attempts"] += 1
            self.stats["unknown_usage_attempts"] += 1
            self.stats["usage_accounting_complete"] = False
            self.stats["reserved_unknown_tokens"] += reservation
            event.update({
                "usage_status": "unknown_reserved", "usage_error": str(error),
                "charged_input_tokens": 0,
                "charged_output_tokens": reservation,
                "unknown_usage_reservation_tokens": reservation,
            })
            return 0, reservation
        event.update(accounting)
        event["usage_status"] = "reported"
        self.stats["reported_total_tokens"] += accounting["reported_total_tokens"]
        reasoning = accounting["reported_reasoning_tokens"]
        if reasoning is not None:
            self.stats["reported_reasoning_tokens"] += reasoning
            self.stats["reasoning_usage_reported_attempts"] += 1
        return accounting["input_tokens"], accounting["output_tokens"]

    def complete(self, messages: list[dict[str, str]], seed: int) -> ModelResponse:
        """RepairAgent has already charged the first call; each retry charges another."""
        self.stats["logical_calls"] += 1
        request_body = {
            "model": self.model, "messages": messages, "temperature": self.temperature,
            "top_p": self.top_p, "max_tokens": self.max_output_tokens,
            "stream": False,
        }
        if self.config.reasoning_effort:
            request_body["reasoning_effort"] = self.config.reasoning_effort
        if self.config.action_mode == "native":
            request_body.update({"tools": self.tools, "tool_choice": "auto"})
        body = json.dumps(request_body, allow_nan=False).encode("utf-8")
        # Conservative reservation for unmetered failed attempts, NOT an exact
        # remote tokenizer measurement. Such episodes cannot certify token caps.
        reservation = len(body) + 256 * (len(messages) + 1) + self.max_output_tokens
        for retry_index in range(MAX_RETRIES + 1):
            self.budget.check()
            if reservation > self.budget.max_tokens - self.budget.total_tokens:
                raise BudgetExceeded("insufficient token headroom for request reservation")
            self._wait(REQUEST_GAP_SECONDS, "pacing_wait_seconds")
            if retry_index:
                if self.budget.model_calls >= self.budget.max_model_calls:
                    raise BudgetExceeded("retry would exceed the model-call cap")
                self.budget.start_model_call()
                self.stats["retries"] += 1
            self.stats["http_attempts"] += 1
            event = {
                "phase": "response_received",
                "attempt": self.stats["http_attempts"],
                "logical_call": self.stats["logical_calls"],
                "retry_index": retry_index,
                "budget_model_calls": self.budget.model_calls,
                "requested_model": self.model, "request_reservation_tokens": reservation,
                "request": request_body, "base_url": self.base_url,
                "action_mode": self.config.action_mode,
                "generation": {
                    "temperature": self.temperature, "top_p": self.top_p,
                    "max_tokens": self.max_output_tokens,
                    "reasoning_effort": self.config.reasoning_effort, "stream": False,
                    "local_experiment_seed": seed, "seed_sent_to_provider": False,
                    "provider_determinism_claimed": False,
                },
            }
            event["request_timeout_seconds"] = min(
                REQUEST_TIMEOUT_SECONDS,
                self.budget.max_active_seconds - self.budget.active_seconds,
            )
            self._record({**event, "phase": "request_started"})
            start = self.clock()
            try:
                status, headers, payload = self.transport.post(
                    body, self._key,
                    min(REQUEST_TIMEOUT_SECONDS,
                        self.budget.max_active_seconds - self.budget.active_seconds),
                )
            except (Exception, KeyboardInterrupt) as error:
                elapsed = max(0.0, self.clock() - start)
                self.stats["request_seconds"] += elapsed
                event.update({"http_status": None, "request_seconds": elapsed,
                              "error": "transport_failed_no_automatic_retry",
                              "transport_error_type": type(error).__name__})
                # A timed-out POST may have generated tokens. Stop and reserve
                # the entire remaining budget; never retry ambiguous delivery.
                charges = self._usage(None, event, self.budget.max_tokens - self.budget.total_tokens)
                charge_error = None
                try:
                    self.budget.charge_model_tokens(*charges)
                except BudgetExceeded as exceeded:
                    charge_error = exceeded
                self._record(event)
                if isinstance(error, KeyboardInterrupt):
                    raise
                if charge_error:
                    raise charge_error
                raise ProviderError("transport_failed_unknown_usage") from None
            elapsed = max(0.0, self.clock() - start)
            self.stats["request_seconds"] += elapsed
            event.update({"http_status": status, "request_seconds": elapsed})
            self.stats["http_429"] += int(status == 429)
            self.stats["http_5xx"] += int(500 <= status <= 599)
            charges = self._usage(payload, event, reservation)
            charge_error = None
            try:
                self.budget.charge_model_tokens(*charges)
            except BudgetExceeded as exceeded:
                charge_error = exceeded
            if 200 <= status < 300:
                try:
                    actual_model = payload.get("model")
                    message = payload["choices"][0]["message"]
                    content = message.get("content")
                    reasoning = message.get("reasoning_content")
                    if isinstance(actual_model, str):
                        clean_model = sanitize(actual_model, self._key)
                        if clean_model not in self.stats["returned_models"]:
                            self.stats["returned_models"].append(clean_model)
                    event.update({
                        "returned_model": actual_model,
                        "finish_reason": payload["choices"][0].get("finish_reason"),
                        "final_answer_text": content if isinstance(content, str) else None,
                        "reasoning_content": reasoning if isinstance(reasoning, str) else None,
                        "final_answer_json": None,
                        "action_protocol": ACTION_PROTOCOL,
                        "native_tool_calls": message.get("tool_calls"),
                        "action_source": "message.content",
                    })
                    native_calls = message.get("tool_calls")
                    if native_calls:
                        if not isinstance(native_calls, list) or len(native_calls) != 1:
                            raise ProviderError("expected_exactly_one_native_tool_call")
                        call = native_calls[0]
                        if not isinstance(call, dict) or call.get("type") != "function":
                            raise ProviderError("invalid_native_tool_call")
                        function = call.get("function")
                        if not isinstance(function, dict) or function.get("name") not in self.tool_names:
                            raise ProviderError("unknown_native_tool_name")
                        arguments = json.loads(function.get("arguments", ""), parse_float=_finite_float, parse_constant=_reject_constant)
                        if not isinstance(arguments, dict):
                            raise ProviderError("native_tool_arguments_must_be_object")
                        content = json.dumps({"action": function["name"], "args": arguments})
                        event["action_source"] = "message.tool_calls"
                        event["normalized_action_text"] = content
                    if isinstance(content, str):
                        try:
                            event["final_answer_json"] = json.loads(
                                content, parse_constant=lambda _: None
                            )
                        except ValueError:
                            pass
                    if not isinstance(actual_model, str) or not actual_model.strip():
                        raise ProviderError("response_model_identity_missing")
                    if self.base_url == DEFAULT_MODEL_BASE_URL and actual_model != self.model:
                        raise ProviderError("response_model_identity_mismatch")
                    if not isinstance(content, str) or not content.strip():
                        raise ProviderError("missing_final_content_reasoning_is_not_an_action")
                    if event["usage_status"] != "reported":
                        raise ProviderError("successful_response_requires_valid_usage")
                except Exception as error:
                    event["error"] = str(error) if isinstance(error, ProviderError) else "invalid_response_schema"
                    event["response_delivered"] = False
                    if charge_error:
                        event["budget_error"] = str(charge_error)
                    self._record(event)
                    if charge_error:
                        raise charge_error
                    raise ProviderError(event["error"]) from None
                event["response_delivered"] = charge_error is None
                if charge_error:
                    event["budget_error"] = str(charge_error)
                self._record(event)
                if charge_error:
                    raise charge_error
                # Usage was charged before normalization/logging; the loop must not charge twice.
                return ModelResponse(sanitize(content, self._key), *charges, usage_charged=True)

            retryable = status == 429 or 500 <= status <= 599
            error_detail = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error_detail, dict):
                event["provider_error"] = {
                    key: value[:2000] if isinstance(value, str) else value
                    for key, value in error_detail.items()
                    if key in {"message", "type", "code", "param"}
                    and isinstance(value, (str, int, float, bool, type(None)))
                }
            wait, source = retry_wait(headers, retry_index)
            event.update({"retryable_status": retryable, "retry_wait_source": source,
                          "retry_requested_wait_seconds": wait})
            if source == "retry-after":
                self.stats["provider_requested_wait_seconds"] += wait
            can_retry = (
                retryable and retry_index < MAX_RETRIES
                and self.budget.model_calls < self.budget.max_model_calls
                and wait <= MAX_RETRY_WAIT_SECONDS
                and wait + REQUEST_GAP_SECONDS < (
                    self.budget.max_active_seconds - self.budget.active_seconds
                )
            )
            event["retry_scheduled"] = can_retry
            if retryable and not can_retry:
                event["retry_stop_bounds"] = {
                    "retry_limit_reached": retry_index >= MAX_RETRIES,
                    "model_call_cap_reached": self.budget.model_calls >= self.budget.max_model_calls,
                    "provider_wait_exceeds_60s_bound": wait > MAX_RETRY_WAIT_SECONDS,
                    "episode_time_insufficient": wait + REQUEST_GAP_SECONDS >= (
                        self.budget.max_active_seconds - self.budget.active_seconds
                    ),
                }
            self._record(event)
            if charge_error:
                raise charge_error
            if not can_retry:
                raise ProviderError(f"http_{status}_stopped_with_bounded_retry_policy")
            self._wait(wait, "provider_wait_seconds")
        raise ProviderError("retry_limit_reached")
