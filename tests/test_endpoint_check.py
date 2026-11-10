import json
import os
import sys
import pytest
from eval import check_endpoint
from harness.model_client import DEFAULT_MODEL_BASE_URL, DEFAULT_MODEL_ID, ModelResponse

@pytest.fixture(autouse=True)
def clear_config(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("HW2_MODEL_", "COURSE_MODEL_")):
            monkeypatch.delenv(name)

def test_missing_key_has_actionable_secret_free_failure():
    status = check_endpoint.environment_status()
    assert status["configured"] is False
    assert "HW2_MODEL_API_KEY" in status["error"]

def test_no_request_uses_nvidia_defaults_and_never_connects(monkeypatch, capsys):
    monkeypatch.setenv("HW2_MODEL_API_KEY", "test-fixture-key")
    monkeypatch.setattr(sys, "argv", ["check", "--no-request"])
    monkeypatch.setattr(check_endpoint, "OpenAICompatibleModelClient", lambda **kwargs: pytest.fail("network client created"))
    check_endpoint.main()
    text = capsys.readouterr().out
    status = json.loads(text)
    assert status["base_url"] == DEFAULT_MODEL_BASE_URL
    assert status["model"] == DEFAULT_MODEL_ID
    assert status["action_mode"] == "native"
    assert "test-fixture-key" not in text

def test_smoke_validates_action_without_running_tool(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HW2_MODEL_API_KEY", "test-fixture-key")
    monkeypatch.setattr(sys, "argv", ["check", "--output-dir", str(tmp_path / "probe")])
    class Client:
        stats = {"http_attempts": 1}
        def __init__(self, **kwargs):
            from harness.model_client import REQUEST_TIMEOUT_SECONDS, REQUEST_GAP_SECONDS
            budget = kwargs["budget"]
            assert budget.max_active_seconds == 220
            assert budget.max_active_seconds > REQUEST_TIMEOUT_SECONDS + REQUEST_GAP_SECONDS
        def complete(self, messages, seed):
            return ModelResponse('{"action":"list_files","args":{"path":"."}}', 10, 20)
    monkeypatch.setattr(check_endpoint, "OpenAICompatibleModelClient", Client)
    check_endpoint.main()
    assert json.loads(capsys.readouterr().out)["action_protocol_ok"] is True


@pytest.mark.parametrize("interrupted", [False, True])
def test_failed_or_interrupted_probe_preserves_api_evidence(monkeypatch, capsys, tmp_path, interrupted):
    from harness import model_client
    monkeypatch.setenv("HW2_MODEL_API_KEY", "test-fixture-key")
    output = tmp_path / "failed-probe"
    monkeypatch.setattr(sys, "argv", ["check", "--output-dir", str(output)])
    monkeypatch.setattr(model_client, "REQUEST_GAP_SECONDS", 0)
    def post(*args, **kwargs):
        if interrupted:
            raise KeyboardInterrupt
        return 401, {}, {}
    monkeypatch.setattr(model_client.HTTPTransport, "post", post)
    with pytest.raises(SystemExit) as caught:
        check_endpoint.main()
    assert caught.value.code == (130 if interrupted else 1)
    assert len((output / "api_attempts.jsonl").read_text().splitlines()) == 2
    assert (output / "provider_metrics.json").is_file()
    assert "test-fixture-key" not in capsys.readouterr().out
