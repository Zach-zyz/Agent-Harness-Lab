# HW2 Release-Day Harness Lab

Allow 60-90 minutes for hands-on work and launching experiments, plus API account
setup and batch time. Use a CPU machine with the isolation described in `README.md`:
macOS `sandbox-exec`, or Linux/WSL2 bubblewrap plus libseccomp. No GPU, Docker, or
self-hosted model is required. There is no separate lab submission.

## Part 1: Install and verify (10 minutes)

From the release root after installing the required OS dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pytest -q
```

The starter tests should pass on a supported host. Missing isolation is an
environment problem, not a model-quality result. Do not bypass a fail-closed error.

## Part 2: Run the deterministic agent loop (10 minutes)

```bash
python -m eval.run_toy
```

Both `toy_parse_bool` and `toy_clamp_zero` should report fresh verification success.
The command saves `.pilot-output/<task>/` artifacts:

```text
patch.diff
trajectory.jsonl
metrics.json
final_state.json
```

Existing output is never overwritten. For a repeat, pass a new directory with
`--output_root runs/toy-second` and use that path in the inspection commands below.

Replay exercises parsing, tool dispatch, writes, tests, stopping, patch construction,
and fresh verification without any API request. It does not measure a hosted model
or substitute for the required public-dev experiments.

## Part 3: Trace one episode (10-15 minutes)

```bash
python -m json.tool .pilot-output/parse_bool/metrics.json
python -m json.tool .pilot-output/parse_bool/final_state.json
sed -n '1,12p' .pilot-output/parse_bool/trajectory.jsonl
sed -n '1,160p' .pilot-output/parse_bool/patch.diff
sed -n '1,20p' examples/successful_trajectory.jsonl
sed -n '1,20p' examples/failed_trajectory.jsonl
```

Locate model, tool, budget, and stopping events. Confirm that the exact designated
visible command ran after the last write and that the patch changes source only.
A local visible `resolved` metric differs from fresh F2P/P2P verification; reporting
resolved within budget also requires complete evidence and certified compliance.
A complete unsuccessful repair remains valid experimental evidence.

## Part 4: Inspect the trust boundary (10 minutes)

Inspect `harness/agent.py`, `harness/tools.py`, and `EVALUATION.md`, then run:

```bash
python -m pytest -q tests/test_tools.py tests/test_verifier.py
```

Public acceptance tests are student-visible for fresh verification but remain
outside the model-facing worktree. Test processes have no API credential or network
access and cannot select alternate pytest configuration. Protected sandbox code
must remain unchanged. Source-policy checks supplement OS isolation.

## Part 5: Plan a controlled change (10 minutes)

Choose a substantive student-owned harness change and state its mechanism.
Plan two single-factor variants, with at least one disabling or replacing your
new mechanism. Disabling bootstrap/context or automatic test feedback is the
provided worked example; adapt the configuration and condition names if your
mechanism instead concerns localization, memory, recovery or another component.
Save the unmodified Naive baseline in Part 7
before implementing changes. A hypothesis can be unsupported by the results and
still earn full analysis credit.

## Part 6: Configure and check your API

Follow `API_SETUP.md` to obtain your own key and set the environment securely.
Default: NVIDIA `https://integrate.api.nvidia.com/v1`, model `openai/gpt-oss-20b`,
native function calling, reasoning effort `low`. Other compatible HTTPS providers
are allowed; `HW2_MODEL_ACTION_MODE=json` is available for content-based actions.
Keep the main provider/model/settings fixed once compatibility is established.

```bash
python -m eval.check_endpoint --no-request
python -m eval.check_endpoint
```

The first check is configuration-only; the second makes a small real API request.
Real probes retain attempt/usage evidence in `outputs/api-check-TIMESTAMP/`,
including failed or interrupted probes. To choose a new directory, use
`python -m eval.check_endpoint --output-dir outputs/api-check-manual-01`.
Existing directories are not overwritten; `--no-request` creates none.
Do not paste credentials into logs or reports. No course-issued key or free quota
is provided. The observed staff account limit of up to 40 requests/minute does
not establish your allowance or a daily quota.

## Part 7: Save public-dev evidence (10 minutes to launch)

Run one task first:

```bash
python -m eval.run_dev \
  --task casefold_headers \
  --output_root runs/dev-smoke \
  --backend endpoint \
  --seed 2026 \
  --config config/default.json
```

Then, before editing the starter, run the 12-task Naive reference:

```bash
python -m eval.run_dev \
  --output_root runs/naive-baseline \
  --backend endpoint \
  --seed 2026 \
  --config config/examples/six_calls.json
```

Naive permits six HTTP attempts and has neither bootstrap context nor automatic
test feedback. Preserve the unchanged config, release checksum, clean starter,
all per-task records, `summary.json`, `tasks.csv`, and `failure_index.md`.

Every HTTP retry consumes an attempt; API latency/pacing/retry waits count toward
1800 active seconds. The token cap is 120,000 provider-reported tokens. Missing
usage is uncertified, and provider errors must not become functional failure
labels. Preserve all failed or interrupted attempts. `--resume` can reuse only
complete records matching run identity and hashes.

## Part 8: Run paired experiments and package (10-15 minutes to launch)

Implement the planned change and rerun unit/toy checks.

Keep HTTP calls serial in the main thread so the protected `SIGALRM` request
deadline works. The API client charges response usage; preserve the
`ModelResponse.usage_charged` guard to avoid charging it twice. Replay responses
remain charged by the agent. The local seed is not sent to the provider.

Evaluate the family. This command shows the context/feedback example; replace its
variants as needed so at least one ablates your own mechanism:

```bash
python -m eval.run_paired \
  --condition full=config/default.json \
  --condition no_context=config/examples/no_context.json \
  --condition no_feedback=config/examples/no_feedback.json \
  --output_root runs/paired-01 \
  --backend endpoint \
  --seed 2026
```

In this example, Full enables both knobs and each variant disables one. All three
conditions retain 12 attempts, 120,000 tokens, 1800 seconds, and identical model/
generation settings. The separate six-attempt baseline is not a causal control
for a feedback-only claim. Minimum coverage is 12 + 36 = 48 episodes.
Equal family caps may be lowered. For an explicitly controlled budget intervention,
pass `--budget-variable` and hold the other factors fixed; see the handout.

Inspect `paired_summary.json`, `paired_summary.md`, `paired_tasks.csv`, and
`conditions/`. Explain paired wins/losses/ties and costs, with uncertainty. Analyze
four failures across conditions, or all failures plus evidence-backed near-misses/
limitations when fewer than four occurred. Never manufacture failures.

If a provider outage requires replacement, keep both attempts and rerun the
affected paired block across all family conditions under the handout policy.
Never retry-drop a failure or cherry-pick a successful replacement.
For a partial study, follow the README's `recovery.json` example and
`tools.submission assemble` / `package --study` commands. The tool creates a new
selection ledger while retaining both histories. Use `--supplement NAME=PATH`
to attach endpoint probes, smoke runs, and incomplete diagnostics; these records
do not count as additional completed slots.

Complete the report, `REPRODUCE.md`, and `ai_disclosure.md`, then:

```bash
python -m tools.submission package \
  --output dist/hw2-submission.zip \
  --report report.pdf \
  --results baseline=runs/naive-baseline \
  --results paired=runs/paired-01

python -m tools.submission check dist/hw2-submission.zip
```

Use `--report report.md` for a Markdown report. Follow the README for added source
packages and evidence retention. TAs use saved patches, evidence, and deterministic
checks without your key or fresh API calls. Zero improvement can earn full points.
