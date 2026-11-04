"""Validate API configuration and make an optional action-protocol smoke request."""
from __future__ import annotations
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from harness.budget import Budget, BudgetExceeded
from harness.model_client import OpenAICompatibleModelClient, ProviderError, resolve_model_config


def environment_status() -> dict[str, object]:
    try:
        config = resolve_model_config()
    except ValueError as error:
        return {"configured": False, "error": str(error)}
    return {"configured": True, "base_url": config.base_url, "model": config.model_id,
            "api_key_present": True, "action_mode": config.action_mode,
            "reasoning_effort": config.reasoning_effort or "provider-default",
            "seed_sent_to_provider": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Check your API key and action protocol; see API_SETUP.md.")
    parser.add_argument("--no-request", action="store_true", help="Validate configuration without an API call.")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--output-dir", type=Path, help="New persistent directory for probe evidence")
    args = parser.parse_args()
    status = environment_status()
    if not status["configured"]:
        print(json.dumps(status, indent=2, sort_keys=True))
        raise SystemExit(2)
    if args.no_request:
        print(json.dumps(status, indent=2, sort_keys=True))
        return
    output = args.output_dir or Path("outputs") / ("api-check-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True, exist_ok=False)
    status["evidence_directory"] = str(output)
    try:
        budget = Budget(max_model_calls=1, max_tokens=8192, max_active_seconds=120)
        client = OpenAICompatibleModelClient(budget=budget, output_dir=output,
                                            temperature=0.0, max_output_tokens=1024)
        try:
            budget.start_model_call()
            response = client.complete([{"role": "user", "content":
                'Select list_files with path ".". Use the function tool if available; '
                'otherwise reply only with {"action":"list_files","args":{"path":"."}}. '
                'This is a protocol test; no tools will actually execute.'}], args.seed)
            if not response.usage_charged:
                budget.charge_model_tokens(response.input_tokens, response.output_tokens)
            action = json.loads(response.content)
            if not isinstance(action, dict) or action.get("action") != "list_files" or not isinstance(action.get("args"), dict):
                raise ValueError("unexpected_action_protocol")
        except (ProviderError, BudgetExceeded, ValueError) as error:
            print(json.dumps({**status, "request_ok": False, "error": str(error),
                "provider": client.stats, "hint": "Check provider key/model and native or JSON compatibility in API_SETUP.md. No generated code was executed."}, indent=2, sort_keys=True))
            raise SystemExit(1) from None
        print(json.dumps({**status, "request_ok": True, "action_protocol_ok": True,
            "input_tokens": response.input_tokens, "output_tokens": response.output_tokens,
            "provider": client.stats}, indent=2, sort_keys=True))
    except KeyboardInterrupt:
        print(json.dumps({**status, "request_ok": False, "error": "interrupted; evidence retained"}, indent=2, sort_keys=True))
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
