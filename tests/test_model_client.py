"""API compatibility, accounting, and credential handling regression tests."""
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.response
from pathlib import Path
from unittest.mock import patch

import pytest
from harness.budget import Budget, BudgetExceeded
from harness.model_client import (
    DEFAULT_MODEL_ID, DEFAULT_MODEL_BASE_URL, APIModelConfig,
    OpenAICompatibleModelClient, HTTPTransport, ProviderError,
    account_usage, retry_wait, sanitize, resolve_model_config,
)

FAKE_KEY = "offline-fixture-credential-not-a-real-key"
ACTION = '{"action":"list_files","args":{"path":"."},"thought":"inspect"}'

def response(content=ACTION, usage=None, model=DEFAULT_MODEL_ID, reasoning="private reasoning"):
    return {
        "model": model,
        "choices": [{"message": {"content": content, "reasoning_content": reasoning},
                     "finish_reason": "stop"}],
        "usage": usage if usage is not None else {
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "completion_tokens_details": {"reasoning_tokens": 15},
        },
    }


class FakeTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.bodies = []

    def post(self, body, key, timeout):
        assert key == FAKE_KEY and 0 < timeout <= 120
        self.bodies.append(json.loads(body))
        item = next(self.responses)
        if isinstance(item, BaseException):
            raise item
        return item


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hw2-client-test-")
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.sleeps = []

    def client(self, transport, budget=None):
        budget = budget or Budget(max_model_calls=6)
        return OpenAICompatibleModelClient(
            config=APIModelConfig(DEFAULT_MODEL_BASE_URL, FAKE_KEY, DEFAULT_MODEL_ID, "native", "low"),
            budget=budget, output_dir=self.output,
            transport=transport, sleep=self.sleeps.append,
        )

    def invoke(self, client):
        client.budget.start_model_call()
        result = client.complete([{"role": "user", "content": "public fixture"}], 2026)
        if not result.usage_charged:
            client.budget.charge_model_tokens(result.input_tokens, result.output_tokens)
        return result

    def test_final_reasoning_usage_and_documented_request_schema(self):
        transport = FakeTransport([(200, {}, response())])
        client = self.client(transport)
        result = self.invoke(client)
        self.assertEqual(result.content, ACTION)
        self.assertEqual(client.budget.total_tokens, 120)
        sent = transport.bodies[0]
        self.assertNotIn("seed", sent)
        self.assertEqual(sent["reasoning_effort"], "low")
        self.assertEqual(sent["max_tokens"], 2048)
        event = json.loads((self.output / "api_attempts.jsonl").read_text().splitlines()[-1])
        self.assertEqual(event["usage_raw"], response()["usage"])
        self.assertEqual(event["reasoning_content"], "private reasoning")
        self.assertEqual(event["final_answer_json"]["action"], "list_files")
        self.assertNotIn("private reasoning", result.content)

    def test_total_and_reasoning_never_undercounted_or_normal_subset_double_counted(self):
        self.assertEqual(account_usage(response()["usage"])["charged_tokens"], 120)
        for usage, minimum in [
            ({"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 33,
              "completion_tokens_details": {"reasoning_tokens": 20}}, 33),
            ({"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
              "reasoning_tokens": 20}, 33),
            ({"prompt_tokens": 10, "completion_tokens": 30, "total_tokens": 60}, 60),
        ]:
            self.assertGreaterEqual(account_usage(usage)["charged_tokens"], minimum)

    def test_native_tool_call_maps_to_action_without_executing_reasoning(self):
        payload = response(content=None, reasoning='{"action":"write_file","args":{}}')
        payload["choices"][0]["message"]["tool_calls"] = [{
            "type": "function", "function": {
                "name": "list_files", "arguments": '{"path":"."}',
            },
        }]
        transport = FakeTransport([(200, {}, payload)])
        client = self.client(transport)
        result = self.invoke(client)
        self.assertEqual(json.loads(result.content), {
            "action": "list_files", "args": {"path": "."},
        })
        self.assertEqual(client.budget.total_tokens, 120)
        self.assertEqual(len(transport.bodies[0]["tools"]), 6)
        self.assertEqual(transport.bodies[0]["tool_choice"], "auto")
        event = json.loads((self.output / "api_attempts.jsonl").read_text().splitlines()[-1])
        self.assertEqual(event["action_source"], "message.tool_calls")
        self.assertIsNone(event["final_answer_text"])
        self.assertEqual(event["final_answer_json"]["action"], "list_files")

    def test_unknown_and_multiple_native_tools_are_rejected_after_accounting(self):
        for calls in [
            [{"type": "function", "function": {"name": "shell", "arguments": "{}"}}],
            [{"type": "function", "function": {"name": "list_files", "arguments": "{}"}}] * 2,
        ]:
            payload = response(content=None)
            payload["choices"][0]["message"]["tool_calls"] = calls
            client = self.client(FakeTransport([(200, {}, payload)]))
            with self.assertRaises(ProviderError):
                self.invoke(client)
            self.assertEqual(client.budget.total_tokens, 120)

    def test_missing_or_malformed_usage_stops_and_preserves_large_known_total(self):
        bad = {"total_tokens": 150000, "prompt_tokens": None, "completion_tokens": 2}
        client = self.client(FakeTransport([(200, {}, response(usage=bad))]))
        with self.assertRaises(BudgetExceeded):
            self.invoke(client)
        self.assertEqual(client.budget.total_tokens, 150000)
        self.assertFalse(client.stats["usage_accounting_complete"])
        self.assertEqual(client.stats["reported_total_tokens"], 150000)

    def test_429_retry_consumes_call_and_reserves_unknown_usage(self):
        transport = FakeTransport([(429, {"Retry-After": "3"}, {}), (200, {}, response())])
        client = self.client(transport)
        self.invoke(client)
        self.assertEqual(client.budget.model_calls, 2)
        self.assertEqual(client.stats["http_attempts"], 2)
        self.assertEqual(client.stats["retries"], 1)
        self.assertEqual(client.stats["unknown_usage_attempts"], 1)
        self.assertGreater(client.budget.total_tokens, 120)
        self.assertIn(3.0, self.sleeps)

    def test_5xx_retry_limit_and_six_call_cap(self):
        transport = FakeTransport([(503, {}, {})] * 5)
        client = self.client(transport)
        with self.assertRaises(ProviderError):
            self.invoke(client)
        self.assertEqual(len(transport.bodies), 3)
        capped = self.client(FakeTransport([(429, {}, {})]), Budget(max_model_calls=6, model_calls=5))
        with self.assertRaises(ProviderError):
            self.invoke(capped)
        self.assertEqual(capped.budget.model_calls, 6)
        self.assertEqual(capped.stats["http_attempts"], 1)

    def test_provider_wait_is_not_shortened_and_400_has_no_parameter_fallback(self):
        for status, headers in [(429, {"Retry-After": "600"}), (400, {})]:
            transport = FakeTransport([(status, headers, {"error": {
                "param": "example-field", "message": "unsupported field " + FAKE_KEY,
            }})])
            client = self.client(transport)
            with self.assertRaises(ProviderError):
                self.invoke(client)
            self.assertEqual(len(transport.bodies), 1)
            last = json.loads((self.output / "api_attempts.jsonl").read_text().splitlines()[-1])
            self.assertEqual(last["provider_error"]["param"], "example-field")
            self.assertNotIn(FAKE_KEY, last["provider_error"]["message"])
        self.assertEqual(retry_wait({"Retry-After": "3"}, 0), (3.0, "retry-after"))

    def test_actual_urllib_transport_with_in_memory_responses(self):
        transport = HTTPTransport(DEFAULT_MODEL_BASE_URL)
        body = json.dumps(response()).encode()
        ok = urllib.response.addinfourl(io.BytesIO(body), {}, "https://example.invalid", 200)
        with patch.object(transport.opener, "open", return_value=ok):
            status, _, payload = transport.post(b"{}", FAKE_KEY, 30)
        self.assertEqual((status, payload["model"]), (200, DEFAULT_MODEL_ID))
        error = urllib.error.HTTPError(
            "https://example.invalid", 429, "limited", {"Retry-After": "5"}, io.BytesIO(b"{}")
        )
        with patch.object(transport.opener, "open", side_effect=error):
            status, headers, payload = transport.post(b"{}", FAKE_KEY, 30)
        self.assertEqual((status, headers, payload), (429, {"retry-after": "5"}, {}))

    def test_wait_is_inside_original_eight_minute_clock(self):
        client = self.client(FakeTransport([]))
        client.budget.started_at -= 479.5
        with self.assertRaises(BudgetExceeded):
            self.invoke(client)
        self.assertEqual(client.stats["http_attempts"], 0)
        self.assertEqual(client.budget.max_active_seconds, 480)

    def test_response_identity_and_reasoning_only_rejected_after_charging(self):
        for payload in [response(model="some-other-model"), response(content=None)]:
            client = self.client(FakeTransport([(200, {}, payload)]))
            with self.assertRaises(ProviderError):
                self.invoke(client)
            self.assertEqual(client.budget.total_tokens, 120)

    def test_transport_exception_cannot_leak_secret_and_is_not_retried(self):
        transport = FakeTransport([RuntimeError("Bearer " + FAKE_KEY)])
        client = self.client(transport)
        with self.assertRaisesRegex(ProviderError, "^transport_failed_unknown_usage$"):
            self.invoke(client)
        self.assertEqual(client.budget.total_tokens, 120000)
        self.assertEqual(len(transport.bodies), 1)
        self.assertNotIn(FAKE_KEY, (self.output / "api_attempts.jsonl").read_text())

    def test_sanitization_before_actions_and_logs(self):
        content = json.dumps({"action": "finish", "args": {"summary": FAKE_KEY}})
        payload = response(content=content, reasoning="Bearer " + FAKE_KEY)
        payload["usage"]["api_key"] = FAKE_KEY
        client = self.client(FakeTransport([(200, {}, payload)]))
        self.assertNotIn(FAKE_KEY, self.invoke(client).content)
        for file in self.output.glob("*"):
            self.assertNotIn(FAKE_KEY, file.read_text())
        self.assertNotIn(FAKE_KEY, json.dumps(sanitize({FAKE_KEY: [FAKE_KEY]}, FAKE_KEY)))

    def test_optional_provider_json_mode_omits_unsupported_fields_and_records_alias(self):
        transport = FakeTransport([(200, {}, response(model="provider/model-version"))])
        client = OpenAICompatibleModelClient(
            config=APIModelConfig("https://api.example.invalid/v1", FAKE_KEY, "other-model", "json", ""),
            budget=Budget(), output_dir=self.output, transport=transport, sleep=lambda _: None,
        )
        self.invoke(client)
        assert transport.bodies[0]["model"] == "other-model"
        assert "tools" not in transport.bodies[0]
        assert "reasoning_effort" not in transport.bodies[0]
        assert "seed" not in transport.bodies[0]
        assert client.stats["returned_models"] == ["provider/model-version"]

    def test_registered_student_tool_extension(self):
        payload = response(content=None)
        payload["choices"][0]["message"]["tool_calls"] = [{
            "type": "function", "function": {"name": "inspect_ast", "arguments": '{"path":"a.py"}'},
        }]
        client = OpenAICompatibleModelClient(
            config=APIModelConfig(DEFAULT_MODEL_BASE_URL, FAKE_KEY, DEFAULT_MODEL_ID),
            budget=Budget(), output_dir=self.output,
            tools=[{"type": "function", "function": {"name": "inspect_ast", "parameters": {
                "type": "object", "properties": {"path": {"type": "string"}}}}}],
            transport=FakeTransport([(200, {}, payload)]), sleep=lambda _: None,
        )
        assert json.loads(self.invoke(client).content)["action"] == "inspect_ast"

    def test_nonfinite_action_diagnostics_do_not_lose_known_usage(self):
        client = self.client(FakeTransport([(200, {}, response(
            content='{"action":"finish","args":{"summary":1e309}}',
        ))]))
        result = self.invoke(client)
        assert result.usage_charged is True
        assert client.budget.total_tokens == 120
        log = (self.output / "api_attempts.jsonl").read_text()
        assert "[NONFINITE]" in log
        assert len(log.splitlines()) == 2

    def test_log_write_failure_still_charges_known_usage(self):
        client = self.client(FakeTransport([(200, {}, response())]))
        original = client._record
        def fail_on_response(event):
            if event["phase"] == "response_received":
                raise OSError("fixture disk write failure")
            original(event)
        with patch.object(client, "_record", side_effect=fail_on_response):
            with pytest.raises(OSError):
                self.invoke(client)
        assert client.budget.total_tokens == client.stats["reported_total_tokens"] == 120

    def test_nonfinite_native_arguments_rejected_after_charging(self):
        payload = response(content=None)
        payload["choices"][0]["message"]["tool_calls"] = [{
            "type": "function", "function": {"name": "list_files", "arguments": '{"path":1e309}'},
        }]
        client = self.client(FakeTransport([(200, {}, payload)]))
        with pytest.raises(ProviderError):
            self.invoke(client)
        assert client.budget.total_tokens == 120


@pytest.fixture
def clean_environment(monkeypatch):
    import os
    for name in list(os.environ):
        if name.startswith(("HW2_MODEL_", "COURSE_MODEL_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HW2_MODEL_API_KEY", FAKE_KEY)
    return monkeypatch


def test_default_and_optional_configuration(clean_environment):
    config = resolve_model_config()
    assert (config.base_url, config.model_id, config.action_mode, config.reasoning_effort) == (
        DEFAULT_MODEL_BASE_URL, DEFAULT_MODEL_ID, "native", "low",
    )
    assert FAKE_KEY not in repr(config)
    clean_environment.setenv("HW2_MODEL_BASE_URL", "https://provider.example/openai/v1/")
    clean_environment.setenv("HW2_MODEL_ID", "vendor/model")
    clean_environment.setenv("HW2_MODEL_ACTION_MODE", "json")
    config = resolve_model_config()
    assert config.reasoning_effort == ""
    assert config.model_id == "vendor/model"
    assert config.base_url == "https://provider.example/openai/v1"


@pytest.mark.parametrize("url", [
    "http://api.example/v1", "https://user:secret@example/v1",
    "https://example/v1?api_key=secret", "https://example/v1#secret",
    "https://example:99999/v1", "https://exa mple/v1", "file:///tmp/key",
])
def test_invalid_endpoint_never_echoes_config_values(clean_environment, url):
    clean_environment.setenv("HW2_MODEL_BASE_URL", url)
    with pytest.raises(ValueError) as caught:
        resolve_model_config()
    assert url not in str(caught.value)
    assert FAKE_KEY not in str(caught.value)


def test_explicit_empty_key_does_not_use_legacy_fallback(clean_environment):
    clean_environment.setenv("COURSE_MODEL_API_KEY", FAKE_KEY)
    clean_environment.setenv("HW2_MODEL_API_KEY", "")
    with pytest.raises(ValueError, match="HW2_MODEL_API_KEY"):
        resolve_model_config()


def test_total_deadline_includes_slow_response_headers():
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    stopped = threading.Event()
    class SlowHeaders(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                self.connection.sendall(b"HTTP/1.1 200 OK\r\n")
                for _ in range(40):
                    if stopped.wait(0.05):
                        return
                    self.connection.sendall(b"X-Slow: value\r\n")
                self.connection.sendall(b"Content-Length: 2\r\n\r\n{}")
            except OSError:
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHeaders)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        transport = HTTPTransport(f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(ProviderError):
            transport.post(b"{}", FAKE_KEY, 0.12)
        assert time.monotonic() - started < 0.8
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_total_deadline_cannot_be_swallowed_by_multi_address_connect(monkeypatch):
    import socket
    import time
    transport = HTTPTransport("http://example.invalid/v1")
    attempts = []
    class SlowSocket:
        def __init__(self, *args, **kwargs):
            pass
        def settimeout(self, timeout):
            pass
        def connect(self, address):
            attempts.append(address)
            time.sleep(0.3)
            raise OSError("fixture failed connection")
        def close(self):
            pass
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 80)),
    ])
    monkeypatch.setattr(socket, "socket", SlowSocket)
    started = time.monotonic()
    with pytest.raises(ProviderError):
        transport.post(b"{}", FAKE_KEY, 0.1)
    assert len(attempts) == 1
    assert time.monotonic() - started < 0.25
