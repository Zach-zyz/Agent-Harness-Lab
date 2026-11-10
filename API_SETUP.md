# HW2 Hosted API Setup

Use your own provider account and API key. The starter defaults to NVIDIA's
`openai/gpt-oss-20b` at `https://integrate.api.nvidia.com/v1`. You do not need a GPU,
model weights, a self-hosted server, or Docker. The course supplies no shared key
or API credits.

## 1. Complete the offline setup

Follow `README.md` to install Python dependencies and the required OS isolation:
built-in `sandbox-exec` on macOS, or bubblewrap plus libseccomp on Linux/WSL2.
The runtime refuses to execute generated code if supported isolation is missing.
Run `python -m pytest -q` and `python -m eval.run_toy` before using an API.

## 2. Set up your account

For the default provider, open [NVIDIA Build](https://build.nvidia.com/), sign in
with your own account, find GPT-OSS-20B, and obtain an API key for the hosted API.
Check current eligibility, pricing, and limits in your account. Do not share keys.

The September 28 staff pilot observed an account limit of **up to 40 requests per
minute**. No daily cap or class-wide free allowance was verified. That observation
does not promise free access, availability, or a particular quota for your account.
Use serial requests and respect the provider's rate limits.

## 3. Configure the harness

From the release root in your activated Python environment:

```bash
export HW2_MODEL_BASE_URL='https://integrate.api.nvidia.com/v1'
export HW2_MODEL_ID='openai/gpt-oss-20b'
unset HW2_MODEL_ACTION_MODE
```

Set `HW2_MODEL_API_KEY` securely in this shell. For bash or zsh, this prompts for
the key without displaying it or putting its value in the command history:

```bash
printf 'API key (input hidden): '
read -r -s HW2_MODEL_API_KEY
printf '\n'
export HW2_MODEL_API_KEY
```

Do not enable shell tracing or print your environment. Do not save the key in
config files, source, a report, or a submission. Test subprocesses must receive
no API credentials.

| Setting | Default or requirement |
|---|---|
| `HW2_MODEL_BASE_URL` | `https://integrate.api.nvidia.com/v1`; HTTPS for remote services, loopback testing exception below |
| `HW2_MODEL_ID` | `openai/gpt-oss-20b` |
| `HW2_MODEL_API_KEY` | Your own nonempty key; no course default |
| `HW2_MODEL_ACTION_MODE` | Native function calling by default; `json` selects content JSON |
| `HW2_MODEL_REASONING_EFFORT` | `low` for the NVIDIA default; record the setting for your provider |
| Provided config sampling | temperature 0.2, top-p 1.0, maximum output 4,096 |

The native mode registers six actual tools and expects one recognized tool call
per action. A reasoning-only answer is not an executable action.

For optional local testing, HTTP is permitted on loopback. Use `localhost`,
`127.0.0.1`, or `[::1]`, which are also accepted by the submission checker.
This exception does not require a local model setup or permit an auxiliary model:
any model used must be the sole designated model for that study.

For an alternative provider, the default reasoning-effort field is omitted; set
`HW2_MODEL_REASONING_EFFORT` only to a supported value (`low`, `medium`, or `high`),
or leave it empty to omit it. Establish this setting before the baseline and keep
it fixed through the main family.

For another compatible provider, change the HTTPS base URL and model ID and use
that provider's key. It must support the released chat-completions interface,
the selected action mode, and usable token-usage reports. An OpenAI-compatible
label alone does not establish compatibility. If needed, select JSON mode:

```bash
export HW2_MODEL_ACTION_MODE=json
```

Usable usage requires nonnegative integer `prompt_tokens`, `completion_tokens`,
and `total_tokens`, with total at least prompt + completion. Missing fields are
invalid, not zero. On an otherwise valid HTTP 2xx response with missing/invalid
usage, the client retains raw usage, marks `unknown_reserved`, charges a
conservative reservation, and raises `successful_response_requires_valid_usage`.
The HTTP attempt counts; no action is delivered and no automatic retry occurs.
The default agent ends the episode as `error` (or `budget_exhausted` if accounting
exceeds a cap). A 429/5xx response may instead receive bounded retries even when
its usage is unknown; any such attempt leaves the episode's token cap uncertified.
A provider that consistently omits valid usage cannot supply usable actions
through this client. Retain the failure evidence and report the incompatibility.

Complete compatibility checks before saving the baseline. Keep provider, model,
action mode, reasoning effort, and generation settings fixed across the main
baseline and family. Do not silently fall back to another model or parameters.
Report unsupported behavior to staff; do not edit the protected client.

## 4. Check configuration and run one task

```bash
python -m eval.check_endpoint --no-request
python -m eval.check_endpoint
python -m eval.run_dev \
  --task casefold_headers \
  --output_root runs/dev-smoke \
  --backend endpoint \
  --seed 2026 \
  --config config/default.json
```

The first check validates configuration without an API request. The second makes
a small request and can consume quota. By default, a real probe saves its API
attempt/usage evidence in `outputs/api-check-TIMESTAMP/` and prints that path as
`evidence_directory`. Failed and interrupted probes retain their evidence. The
configuration-only `--no-request` check does not create a probe directory.

To name a probe's evidence directory explicitly, use a new path:

```bash
python -m eval.check_endpoint --output-dir outputs/api-check-manual-01
```

An existing output directory is rejected; preserve it and choose a new name for
another attempt. The public-task smoke run checks the complete repair workflow.
Keep failed probes and runs as well as successful ones.

Run the HTTP client serially in the process's main thread. Its total request
deadline uses `SIGALRM` to cover DNS, connection, TLS, response headers, and body.
Worker-thread requests and conflicting active process alarms are rejected; do not
disable this deadline or parallelize client calls. The local `--seed` is recorded
in evidence but is **not sent in API requests**.

Inspect the trajectory, patch, metrics, and fresh verification before launching
the 12-task baseline. A successful endpoint probe alone does not establish that
tool calls, longer contexts, or all tasks work.

## 5. Handle errors and finish

The per-request HTTP deadline is 200 seconds. The endpoint probe has a separate
220-second active-time budget, leaving room for pacing and accounting. Each real
task still has a 1800-second active-time cap; the HTTP deadline is clipped to its
remaining budget. Increasing the request deadline does not increase task budgets.
Attempt records include `request_timeout_seconds` (the timeout ceiling before
logging; the actual transport timeout is clipped again to remaining time) and,
on transport failure, `transport_error_type`. Exception messages are omitted to
avoid exposing credentials; a generic transport error type alone does not identify
whether the underlying problem was networking or provider latency. Retain failed
probes and retry with a new evidence directory after checking connectivity and
provider availability. Ambiguous transport failures are not automatically retried.

- **Authentication/permission errors:** check account access and the key locally;
  share only redacted errors with staff.
- **Rate limits or provider outages:** preserve the error and every HTTP attempt.
  Retry waits and API latency count toward 1800 active seconds; retries consume the
  same six/twelve-attempt episode cap. Follow the handout's paired-block rerun
  policy, keeping both attempts.
- **Unsupported parameters/actions:** retain the response and chosen mode; do not
  execute reasoning text or silently change the experiment.
- **Missing/invalid usage:** label token compliance uncertified. A patch pass
  does not turn unknown usage into a certified within-budget result.
- **Isolation unavailable:** install the supported OS dependencies or use a
  supported machine. Do not disable the sandbox to make a run continue.

When finished with API work:

```bash
unset HW2_MODEL_API_KEY
```

TAs grade saved evidence, patches, and deterministic local checks. They do not
need your key or a fresh API run. A local seed records an experimental setting;
it does not guarantee repeated provider outputs.
