# HW2 API Model Contract

## Defaults and choice

The October 1, 2026 release uses the following starting configuration. Other
compatible HTTPS providers/models are allowed; fix your choice before the main
baseline and keep it fixed throughout the required paired family.

| Field | Starter default |
|---|---|
| Provider/base URL | NVIDIA, `https://integrate.api.nvidia.com/v1` |
| Model ID | `openai/gpt-oss-20b` |
| Credential | Student's own `HW2_MODEL_API_KEY` |
| Request API | `POST <HW2_MODEL_BASE_URL>/chat/completions` |
| Action protocol | Native function calling, six actual tools |
| Optional content protocol | `HW2_MODEL_ACTION_MODE=json` |
| NVIDIA reasoning effort | `low` |
| Temperature / top-p | 0.2 / 1.0 in all provided required configs |
| Maximum output | 2,048 in all provided required configs |
| Local experiment seed | `2026` in examples; recorded locally and not sent in API requests |

`HW2_MODEL_BASE_URL` and `HW2_MODEL_ID` select a different compatible provider/model.
Use that provider's own key. Remote services require HTTPS; optional local testing
may use HTTP on loopback (`localhost`, `127.0.0.1`, or `[::1]`). A model behind that
endpoint must be the sole designated model for the study, not an auxiliary model
alongside it. Do not silently change model/settings after an error.
Consult `API_SETUP.md` for setup.
No GPU, self-hosted server, Docker, model download, or shared course credential is
required. The course provides no API credits.

`HW2_MODEL_REASONING_EFFORT` can select a supported reasoning setting; the client
defaults to `low` at the NVIDIA URL and otherwise omits the field unless configured.
Keep it fixed in the main study. `GRADING_POLICY.json` records the current grading
categories and hard caps.

## Compatibility and actions

The provider must accept the released chat-completions request and chosen action
mode, return usable model/action responses, and report token usage. Native mode
registers `list_files`, `read_file`, `search`, `write_file`, `run_tests`, and
`finish` separately. Exactly one recognized call with object arguments becomes
one normalized action. Multiple/unknown calls or malformed arguments are logged
errors. Valid final-content action JSON is also supported by the starter adapter.
Reasoning is retained separately and is never substituted for an action.

The September 28 NVIDIA pilot found that the original JSON-only repair prompt
could return HTTP 200 with no final content and an action inside reasoning.
Native function calling returned usable actions. This is the evidence for the
native default; arithmetic probes alone did not establish repair compatibility.

A provider's compatibility label does not establish support for every setting.
Record the requested and returned model identity, action mode, reasoning effort,
and actual generation settings. Record any exposed provider revision/fingerprint
and run timestamps; mark unavailable version information as unavailable. Complete
compatibility work before the baseline, without editing protected client code.

## Budgets and usage

Run HTTP requests serially in the main thread. The client uses `SIGALRM` for a
total deadline covering DNS/connect/TLS/headers/body and rejects worker-thread
execution or a conflicting active process alarm. Preserve this behavior when
editing the student-owned agent loop.

The hard caps are 12 HTTP attempts, 120,000 tokens under protected-client
accounting, and 480 active elapsed seconds per episode. The immutable
`six_calls.json` reference lowers the attempt cap to six. Retries, failed HTTP
requests, and transport attempts consume the attempt allowance. There is no
separate retry allowance outside that cap.

The API client charges response usage against the shared budget itself, including
usage on error paths. A returned `ModelResponse` sets `usage_charged=True`; the
agent must not charge that usage again. Replay responses have
`usage_charged=False`, so the agent charges their recorded input/output tokens.
Preserve the starter's conditional `if not response.usage_charged` when adapting
the loop. The agent charges the initial attempt before calling the client; the
client charges any retries. Do not add or remove charges at either boundary.

Preserve raw provider usage as well as normalized prompt, completion, reasoning
(if reported), and total counts. The protected client charges all reported total
usage, including usage returned with errors. Ordinary
`completion_tokens_details.reasoning_tokens` counts are completion subsets.
For an ambiguous top-level `reasoning_tokens` count, or a nested count larger
than completion, the client charges the larger of the reported total and
prompt + completion + reasoning. It records the adjustment in `accounting_notes`
as `reasoning_inclusion_ambiguous_conservative_addition`. This can conservatively
overcount included reasoning; it enforces a safety cap, not a provider bill.
An absent reasoning detail is not evidence of zero reasoning.

Missing/invalid usage makes actual token compliance uncertified. The client
records an unknown-usage reservation and rejects otherwise successful responses
without valid usage; the default agent stops without executing their actions.
`API_SETUP.md` describes required fields and error/retry handling. Reservations
are not provider measurements or certified bounds.

Active time includes API latency, pacing, rate-limit/retry sleeps, local tools,
visible tests, and patch construction. Setup precedes the timer. Fresh verification
has a separate duration. Keep over-budget, unknown-usage, provider-error, and
incomplete records; do not report them as ordinary functional failures or silently
exclude them from task coverage. The handout defines documented outage reruns.

Common caps limit exposure; they do not equalize model compute or cost. Provider
tokenizers, reasoning accounting, prices, and latency differ. Hold caps fixed
within a controlled family unless budget is explicitly the experimental variable.
The supplied context/feedback example uses equal caps.

## Access evidence and limits of the pilot

On September 28, one staff account displayed **up to 40 requests/minute**. A daily
cap was not verified. No class-wide free allowance, continuing availability, or
free access through the due date is promised. Students check their own accounts.

The staff pilot resolved 9/12 public tasks with the six-attempt Naive configuration
and 10/12 with a twelve-attempt feedback configuration. These are illustrative
observations under the pilot runtime, not grade thresholds or a controlled causal
estimate: call caps and harness behavior differed. `BASELINES.md` provides the
costs and limitations. The current release does not claim fresh measurements for
every provider or every revised configuration.

## Reproducibility and grading

A hosted model ID does not pin weights, tokenizer, chat template, or serving engine.
Record what the provider exposes and avoid unsupported claims about a training
cutoff, context capacity, checkpoint identity, or deterministic output. Local seed
2026 is an experiment record and is not sent to the API. Temperature
zero would not establish a guarantee either.

Preserve release/runtime/task/config hashes, dependency and OS versions, exact
commands, all requests/responses and usage, patches, and verification. Hold
provider/model/sampling/action/reasoning settings fixed across the main baseline
and family. If changing them, begin a new complete main study and retain the old
attempts; exploratory model comparisons do not replace the required study.

Staff grade the saved patches and evidence, code, report, replay/toy behavior,
and deterministic local harness checks without students' keys. No new API run,
private evaluation submission, or hidden performance score is required. Zero
improvement can receive full credit under the 25/30/30/15 rubric.
