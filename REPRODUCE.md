# HW2 Reproduction Record

No API key is needed to check this submission. Grading uses the saved patches,
trajectories, and the offline commands in Section 2. Repeating the API experiments
(Section 3) needs the runner's own key, uses paid quota, and is not expected to
reproduce the exact model text.

## 1. Environment and provenance

| Item | Value |
|---|---|
| Starter archive SHA-256 | `85f7a6443932204d310eb3ae4c217e23c50cce473d8f8b0f8aeb2c37efb96d81` (`cse8803-hw2-student.zip`, recorded in `provenance/starter_sha256.txt`) |
| Clean starter in git | commit `07d215e` ("Initial Commit"), tag `clean-starter` |
| Submitted harness | commit `2fb87d9` (mechanism added in `2adcc0d`, visible-test pack in `2fb87d9`) |
| Baseline source identity | `source_sha256` `017262c7cd757179892df908072b42c9f7a1f89c71e8c5f9e1b5ec165d9fb9d7` |
| Family source identity | `source_sha256` `f0b32f5308c66b0641c772c6bfdfb8e15e5d7ca0422fc2ec7f04a044a0e1552e` (same for all three conditions) |
| Config SHA-256 | baseline `six_calls.json` `0e7c1f91…82e5`; `full.json` `c6790787…b9a`; `no_localize.json` `4dfd893a…afdf`; `no_feedback.json` `63d5f249…5173` (full values in each `episode_manifest.json`) |
| Task inputs | 12 public dev tasks, `task_input_sha256` recorded per episode in `episode_manifest.json` |
| Python | CPython 3.12.14 (uv-managed build, `.venv`), executable SHA-256 `2d96eb82…c22` |
| Dependencies | `requirements.lock` (unchanged); pytest 9.0.2 |
| OS / machine | macOS 26.6.2 (build 25G83), arm64 (Apple Silicon) |
| Isolation backend | macOS `/usr/bin/sandbox-exec` (selected by `harness/sandbox.py`) |
| GPU | not used |

**Isolation check.** The starter's unit suite (`python -m pytest -q`, 568 passed)
includes the sandbox tests. `python -m eval.run_toy` passed fresh verification on both
toy tasks, both before and after my changes (`runs/toy-after-change-02`,
`runs/toy-after-change-03`).

**Source snapshots match git.** The runner copies the source it used into
`<run>/sources/<source_sha256>/`. I compared every file in these copies with git:

- the baseline snapshot is byte-identical to commit `07d215e`;
- the family snapshot is byte-identical to commit `2fb87d9`.

**Runtime changes.** All graded episodes (baseline and family) ran with the same
Python build, dependencies, OS version, and machine. There were no runtime changes
between batches.

**Model and generation settings (all graded runs).**

| Setting | Value |
|---|---|
| Provider / base URL | OpenRouter, `https://openrouter.ai/api/v1` |
| Requested model / returned model | `openai/gpt-oss-20b` / `openai/gpt-oss-20b` (in `metrics.json` → `provider.returned_models`) |
| Model revision / fingerprint | not exposed by the provider (unavailable) |
| Action mode | `native` (six function tools; `HW2_MODEL_ACTION_MODE` unset) |
| Reasoning effort | `low` |
| Temperature / top-p / max output | 0.2 / 1.0 / 4096 |
| Local seed | 2026 |

The released client records the seed locally (`local_experiment_seed: 2026`) and
does not send it to the provider (`seed_sent_to_provider: false`,
`provider_determinism_claimed: false` in every `api_attempts.jsonl`). API outputs
are therefore not deterministic, and a rerun may produce different patches.

**Timestamps (local time, ET).**

| Run | Date |
|---|---|
| Groq and OpenRouter endpoint probes | 2026-11-09 |
| `runs/dev-smoke` (starter, `config/default.json`) | 2026-11-09 |
| `runs/naive-baseline` (12 episodes) | 2026-11-09 |
| `runs/smoke-full-01` (submitted harness, `full.json`) | 2026-11-15 |
| `runs/paired-01` (36 episodes) | 2026-11-15 |

## 2. Offline checks (no API key)

From a clean extracted release with this submission's source:

```bash
python --version                  # 3.12.x
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock

python -m pytest -q               # unit checks (568 passed on my machine)
python -m harness.check_localize  # deterministic check of my mechanism
python -m eval.run_toy --output_root runs/toy-check

python -m tools.submission check dist/hw2-submission.zip
python -m tools.submission reverify dist/hw2-submission.zip \
  --output runs/reverification-01
```

`harness.check_localize` should print `check_localize: all checks passed`.
`reverify` re-applies every saved patch to a fresh public repository and writes new
verdicts without any model call.

## 3. API experiments (optional repetition, needs your own key)

Set the API using variable names only. Never write the key into a file:

```bash
export HW2_MODEL_BASE_URL='https://openrouter.ai/api/v1'
export HW2_MODEL_ID='openai/gpt-oss-20b'
export HW2_MODEL_REASONING_EFFORT='low'
unset HW2_MODEL_ACTION_MODE
read -r -s HW2_MODEL_API_KEY && export HW2_MODEL_API_KEY

python -m eval.check_endpoint --no-request
python -m eval.check_endpoint --output-dir outputs/api-check-openrouter-01
```

**Baseline.** Run this on the unchanged starter (`git checkout clean-starter`); the
config must not be modified.

```bash
python -m eval.run_dev \
  --output_root runs/naive-baseline \
  --backend endpoint --seed 2026 \
  --config config/examples/six_calls.json \
  2>&1 | tee runs/naive-baseline.log
```

**Primary family.** Run this on the submitted harness (commit `2fb87d9`). First a
one-task smoke run:

```bash
python -m eval.run_dev --task toposort_stable \
  --output_root runs/smoke-full-01 \
  --backend endpoint --seed 2026 \
  --config config/experiments/full.json
```

Then the 36 paired episodes:

```bash
caffeinate -i python -m eval.run_paired \
  --condition full=config/experiments/full.json \
  --condition no_localize=config/experiments/no_localize.json \
  --condition no_feedback=config/experiments/no_feedback.json \
  --output_root runs/paired-01 --backend endpoint --seed 2026 \
  2>&1 | tee runs/paired-01.log

unset HW2_MODEL_API_KEY
```

**Packaging:**

```bash
tectonic report.tex   # produces report.pdf
python -m tools.submission package \
  --output dist/hw2-submission.zip \
  --report report.pdf \
  --results baseline=runs/naive-baseline \
  --results paired=runs/paired-01 \
  --supplement api_check_groq=outputs/api-check-groq-01 \
  --supplement api_check=outputs/api-check-openrouter-01 \
  --supplement smoke=runs/dev-smoke \
  --supplement smoke_full=runs/smoke-full-01
python -m tools.submission check dist/hw2-submission.zip
```

No partial-study recovery was needed, so `tools.submission assemble` and
`recovery.json` were not used.

## 4. Conditions and artifact map

Each family condition changes exactly one key relative to Full:

| Condition | Config | Difference from Full |
|---|---|---|
| Baseline (reference) | `config/examples/six_calls.json` on starter `07d215e` | 6 calls, `bootstrap_context` false, `auto_test_after_write` false, no `localized_context` key, starter code |
| `full` | `config/experiments/full.json` | — (12 calls / 120,000 tokens / 1,800 s; all three switches true) |
| `no_localize` | `config/experiments/no_localize.json` | `localized_context: false` (ablates my mechanism) |
| `no_feedback` | `config/experiments/no_feedback.json` | `auto_test_after_write: false` (ablates a starter component) |

| Evidence | Path |
|---|---|
| Baseline batch | `runs/naive-baseline/` (`summary.json`, `tasks.csv`, `failure_index.md`, one directory per task) |
| Family summary | `runs/paired-01/paired_summary.{md,json}`, `runs/paired-01/paired_tasks.csv` |
| Family per condition | `runs/paired-01/conditions/<condition>/` (`summary.json`, `tasks.csv`, `failure_index.md`) |
| Per episode | `<batch>/<task>/` (`episode_manifest.json`, `metrics.json`, `provider_metrics.json`, `api_attempts.jsonl`, `trajectory.jsonl`, `final_state.json`, `patch.diff`, `verification.json`) |
| Report Tables 2–3 and Appendix A | computed from the `tasks.csv`, `metrics.json`, and `verification.json` files above |
| Report case studies 1–4 | `paired-01/conditions/full/casefold_headers`, `full/json_pointer` (plus `naive-baseline/json_pointer`), `full/toposort_stable`, `no_feedback/retry_after` |
| Mechanism source | `harness/localize.py`, `harness/agent.py` (lines 110–117), `run_agent.py` (lines 98, 184), `harness/check_localize.py` |

**Preserving the clean starter.** Before any edit, I recorded the archive checksum
and committed the extracted release as `07d215e`, tagged `clean-starter`. The
baseline ran on that unmodified code, and its source snapshot matches the commit.

## 5. Attempts, budgets, and accounting

**Caps.** The baseline allows 6 HTTP attempts. The family allows 12 HTTP attempts,
120,000 tokens, and 1,800 active seconds per episode. The baseline also has
120,000-token and 1,800-second caps.

**How usage is counted.** Each HTTP attempt, including any retry, counts as one
model call. Active time includes the client's pacing waits, which are about 2 s per
call, recorded as `pacing_wait_seconds` in `metrics.json`. Active time does not
include fresh verification.

**What happened during the runs.**

- **Retries and errors:** There were 0 retries, 0 HTTP 429 responses, and 0 HTTP
  5xx responses in all 48 graded episodes.
- **Unknown usage:** 0 attempts had unknown usage. Every episode has
  `usage_accounting_complete: true` and `token_cap_certified: true`.
- **Reported versus charged tokens:** Charged tokens equal provider-reported totals
  in 47 of 48 episodes. In `paired-01/conditions/full/toposort_stable`, the
  provider reported 3,883 tokens, and the protected client charged 4,451 as a
  conservative reasoning-usage addition. The raw provider usage is kept unchanged
  in `api_attempts.jsonl`.
- **Reservations:** The client reserves `request_reservation_tokens` before each
  call. Reservations are a planning limit, not billed tokens.

**Cost.** The source is the `cost` field that OpenRouter returns in each response,
in USD. I did not rely on any free quota.

| Run | Cost (USD) |
|---|---:|
| Baseline | 0.0073 |
| Family (36 episodes) | 0.0151 |
| `runs/dev-smoke` | 0.00018 |
| `runs/smoke-full-01` | 0.00014 |
| OpenRouter probe | 0.00001 |
| **Total** | **≈ 0.0227** |

**Additional attempts, interruptions, and replacements.** All of these are kept on
disk. Only the baseline and `paired-01` enter the main tables.

| Record | Date | What happened | Handling |
|---|---|---|---|
| NVIDIA web playground | 2026-11-09 | Requests timed out in the browser. No harness probe was run, so no artifact exists. | Switched provider before any graded run. |
| `outputs/api-check-groq-01` | 2026-11-09 | Groq endpoint probe returned HTTP 403 after about 56 ms, with 0 tokens. The suspected cause is an edge block of the client's user agent. The protected client was not modified. | Kept as a supplement. Switched to OpenRouter. |
| `outputs/api-check-openrouter-01` | 2026-11-09 | Successful probe. | Kept as a supplement. |
| `runs/dev-smoke` | 2026-11-09 | Starter smoke run, 1 task. | Kept as a supplement. Not in the main tables. |
| `runs/toy-after-change` | 2026-11-15 | Invalid run: started by mistake with a non-project Python 3.14 interpreter, which failed with "No module named pytest". No API calls were made. | Kept on disk, not packaged. Rerun offline as `runs/toy-after-change-02` with the `.venv` 3.12.14. |
| `runs/toy-after-change-02`, `-03` | 2026-11-15 | Offline replay checks after each code change. Both toy tasks passed. | Offline only. Not in the main tables. |
| `runs/smoke-full-01` | 2026-11-15 | 1-task smoke run of the submitted harness (`toposort_stable`, unresolved). | Kept as a supplement. Not in the main tables. |

There were no interrupted batches, no resumed batches, no provider-outage
replacement blocks, and no reruns of any graded episode.

The one provider error in the family, `no_feedback/retry_after`, ended with
`ProviderError: missing_final_content_reasoning_is_not_an_action`. It is reported
as recorded, as an error outcome, and was not replaced. All failed repairs remain in
the record.

**Commit timing.** The family was run from the working tree before I made the final
commit. The visible-test addition to `harness/localize.py` and
`harness/check_localize.py` was committed as `2fb87d9` after the run finished. No
code changed between the run and the commit: the run's recorded source snapshot
(`f0b32f…`) is byte-identical to commit `2fb87d9`.
