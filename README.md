# CSE 8803 HW2 Student Release

Read `HANDOUT.md` for the authoritative assignment and `HARNESS_LAB.md` for the
release-day tutorial. This release contains two replay tutorials, 12 public-dev
tasks, batch/paired runners, protected API/budget/isolation code, submission tools,
and a fresh-repository F2P/P2P verifier.

Use your own API account and key. The default is NVIDIA at
`https://integrate.api.nvidia.com/v1`, model `openai/gpt-oss-20b`, native function
calling, and low reasoning effort. Compatible HTTPS providers/models are allowed.
No GPU, self-hosted model, or Docker is required; no shared keys or course API
credits are provided. See `API_SETUP.md` and `MODEL_CARD.md`.

## 1. Install and check safe execution

Use Python 3.11+ and Git on macOS, Linux, or Linux under WSL2. Native Windows Command
Prompt and PowerShell are not supported. Generated-code tests require OS isolation:

- macOS: the built-in `/usr/bin/sandbox-exec`;
- Linux/WSL2: bubblewrap and libseccomp, with unprivileged sandbox creation enabled.

On Debian/Ubuntu, install the Linux system packages before running the tests:

```bash
sudo apt-get update
sudo apt-get install bubblewrap libseccomp2
```

If your host disallows the required isolation, the runtime fails closed. Use a
supported host or obtain administrator help; do not disable isolation. A Python
source-policy check is not a substitute for OS isolation.

On Ubuntu 24.04, an AppArmor user-namespace restriction can prevent `bwrap` from
starting even when its packages are installed. If this occurs, an administrator
can inspect and install the supplied profile scoped to `/usr/bin/bwrap`:

```bash
sudo install -m 0644 tools/bwrap.apparmor /etc/apparmor.d/cse8803-bwrap
sudo apparmor_parser -r /etc/apparmor.d/cse8803-bwrap
```

This prerequisite was tested on Ubuntu 24.04 with bubblewrap 0.9.0. It permits
Bubblewrap to create its sandbox; the HW2 filesystem, network, process and resource
restrictions still apply. Keep AppArmor enabled and retain global user-namespace
restrictions. On a managed machine, ask its administrator to configure this
prerequisite or use another supported host. WSL2 requires compatible Linux
namespaces but was not separately tested.

From the release root:

```bash
git --version
python --version
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pytest -q
python -m eval.run_toy
```

Replay uses no model API. Both toy tasks should pass fresh verification. Episodes
write `patch.diff`, `trajectory.jsonl`, `metrics.json`, and `final_state.json`.
The replay checks control flow; it does not substitute for public-dev API runs.
The toy command preserves existing output: for another run choose a new directory,
for example `python -m eval.run_toy --output_root runs/toy-second`.

## 2. Check the API and save the immutable baseline

Follow `API_SETUP.md` to set your own `HW2_MODEL_API_KEY` securely. Set
`HW2_MODEL_BASE_URL` and `HW2_MODEL_ID` if selecting another compatible provider.
The default action mode uses six native function tools. Set
`HW2_MODEL_ACTION_MODE=json` only if using the compatible content-JSON mode.

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

Complete compatibility setup before the baseline. Fix provider/model, temperature,
top-p, output limit, action mode, and reasoning settings across the main study.
The provided configs use temperature 0.2, top-p 1.0, and maximum output 2,048.
A local seed and deterministic task order do not guarantee deterministic API output.

The real endpoint probe retains API-attempt/usage evidence by default in
`outputs/api-check-TIMESTAMP/`, including failures and interruptions. Use
`python -m eval.check_endpoint --output-dir outputs/api-check-manual-01` to select
a new path; existing directories are rejected. `--no-request` creates no probe
directory. The CLI reports the retained evidence location.

Before editing the starter, run the provided Naive configuration on all 12 tasks:

```bash
python -m eval.run_dev \
  --output_root runs/naive-baseline \
  --backend endpoint \
  --seed 2026 \
  --config config/examples/six_calls.json
```

Keep `config/examples/six_calls.json` unchanged. It uses six HTTP attempts, no
bootstrap context, and no automatic test feedback. Preserve a clean starter copy,
release checksum, and this run's evidence before modifying your harness.

Each batch saves per-task artifacts/verification plus `summary.json`, `tasks.csv`,
and `failure_index.md`. Keep additional API-attempt, identity, receipt, and error
files emitted by the runner. Public `grader_tests/` are verifier inputs and are
never copied into model-facing worktrees.

## 3. Implement and run the three-condition family

You may modify `run_agent.py`, `harness/agent.py`, `harness/tools.py`,
`harness/localize.py`, `harness/state.py`, `harness/patch.py`, prompts, and experiment
configs other than the immutable Naive config. You may add source modules and
extend tool schemas while preserving the client interface, required CLI/artifacts,
parseable trajectories, budget boundary, and prohibited capabilities.

Implement at least one substantive mechanism of your own, such as localization,
context/state representation, tool behavior or recovery. Merely selecting a model
or toggling the supplied configuration is not this implementation requirement.
Identify the source changes and a deterministic check that exercises the mechanism;
it need not improve resolve rate.

Keep HTTP calls serial in the main thread; their total deadline uses `SIGALRM`,
and worker threads or conflicting process alarms are unsupported. The local seed
is retained in evidence but not sent in API requests. The API client charges usage
before returning `ModelResponse.usage_charged=True`; the agent must not charge it
again. Replay returns `usage_charged=False` and is charged by the agent. Preserve
the starter's accounting guard when changing the loop.

Do not change sandbox controls, `harness/budget.py`, `harness/model_client.py`,
`eval/`, task tests, or `requirements.lock`. Re-run the unit and toy
checks after implementation changes.

Your primary family contains exactly three conditions: Full and two single-factor
variants; at least one contrast ablates your own mechanism. The following supplied
configurations are a worked example for a context/feedback study. Other components
and condition names are allowed when the hypothesis and intervention are explicit:

| Config | Bootstrap context | Automatic tests after writes | HTTP-attempt cap |
|---|---|---|---:|
| `config/default.json` | on | on | 12 |
| `config/examples/no_context.json` | off | on | 12 |
| `config/examples/no_feedback.json` | on | off | 12 |

```bash
python -m eval.run_paired \
  --condition full=config/default.json \
  --condition no_context=config/examples/no_context.json \
  --condition no_feedback=config/examples/no_feedback.json \
  --output_root runs/paired-01 \
  --backend endpoint \
  --seed 2026
```

Each variant changes one factor relative to Full; keep all other code/settings
and budgets identical. The example uses 120,000 tokens and 480 active seconds. If
you change the full config, synchronize shared settings in its two variants.
Other mechanisms must have explicit, single-factor interventions. You may lower
the equal family caps. If a budget itself is an experimental variable, add
`--budget-variable` to `eval.run_paired` and explain the controlled budget contrast;
all caps must remain within the hard limits. At least one contrast must still
ablate your implemented mechanism. Model changes belong in separate optional studies.

Top-level outputs include `paired_summary.json`, `paired_summary.md`,
`paired_tasks.csv`, and `conditions/`. This family is separate from the saved
unmodified Naive reference: **12 + 3 × 12 = 48 required episodes**. The separate
six-attempt baseline is not a same-budget control for feedback effects.

Run additional conditions as separate exploratory studies and retain them with
`package --supplement NAME=PATH`, outside primary coverage/aggregates. If an
existing run already contains extra conditions, preserve it intact: use the
Section 5 assembly plan to name only the predeclared Full and two primary variants,
and attach the original run as a supplement. Do not select the primary conditions
based on their outcomes.

## 4. Keep complete and interpretable evidence

The hard caps are **12 HTTP attempts including retries, 120,000 tokens under
protected-client accounting, and 480 active elapsed seconds per episode**;
Naive lowers attempts to six.
API latency, pacing, retry waits, tools, visible tests, and patch construction
count toward active time. Fresh verification time is separate. These are safety
caps, not equivalent compute or spending across models.

Retain every attempt and outcome, including failed repairs, provider errors,
source-policy rejections, unknown usage, and incomplete records. Missing usage
makes the token cap uncertified. Never label a provider or verifier infrastructure
error as a functional failure, silently drop retries, or overwrite failure evidence.
`--resume` may reuse only complete artifacts with matching recorded identities and
hashes; use a new output root for changed conditions or documented replacements.

Python patch updates, replacement Python binaries, and OS-version updates do not
by themselves invalidate resume or paired comparisons. Python major.minor,
implementation, OS family, machine architecture, and dependency versions must
still match; source/config/task/model/settings checks remain strict. Each episode
retains its full original runtime record. Disclose runtime changes in
`REPRODUCE.md`; never edit saved identities or receipts to make them match.

A documented provider outage permits a replacement of the affected task block in
all family conditions, with original and replacement evidence retained. Explain
the outage, selection rule, and which full block is in the main table. Never choose
runs by success. For the independent baseline, preserve any corresponding replaced
slots as well. See `HANDOUT.md` for the full policy.

Report all task outcomes, paired wins/losses/ties, budgets, and uncertainty. A
one-task difference on 12 tasks is not proof of a component's benefit. Analyze four
failure cases across conditions, or all failures plus evidence-backed near-misses
or limitations if there are fewer than four. Cite trajectories and never invent
failures. A null or negative improvement can earn full credit.

## 5. Package and independently check

Complete `ai_disclosure.md`, `REPRODUCE.md`, and the 4-5 page technical report
(excluding references/appendix). PDF or Markdown is accepted; use `--report report.md`
for Markdown and keep its rendered length comparable.

```bash
python -m tools.submission package \
  --output dist/hw2-submission.zip \
  --report report.pdf \
  --results baseline=runs/naive-baseline \
  --results paired=runs/paired-01

python -m tools.submission check dist/hw2-submission.zip
```

Attach endpoint probes, smoke runs, interrupted records, and optional studies with
`--supplement NAME=PATH`, repeated as needed. For example, add
`--supplement api_check=outputs/api-check-01` and
`--supplement smoke=runs/smoke-01` to the package command. Supplemental evidence is
retained for review but does not add completed slots or enter the main aggregates.
Include diagnostic files, not keys or generated task worktrees.

### Recover a partial study without overwriting it

Use a new output root to finish missing tasks or replace a documented outage block.
`eval.run_dev --task TASK_ID` runs one task; `eval.run_paired --task TASK_ID` runs
that task across the declared conditions. Repeat `--task` for multiple tasks and
keep the original configs, source, model settings, and seed. Replacing a family
task requires its entire three-condition block, regardless of repair outcomes.
Complete an interrupted Naive baseline from the preserved, unmodified starter.

Save a `recovery.json` plan next to the `runs/` directory. This example replaces
one affected task; omit either replacement list when it is unnecessary:

```json
{
  "format_version": 1,
  "baseline": "runs/naive-baseline",
  "conditions": {
    "full": "runs/paired-01/conditions/full",
    "no_context": "runs/paired-01/conditions/no_context",
    "no_feedback": "runs/paired-01/conditions/no_feedback"
  },
  "condition_order": ["full", "no_context", "no_feedback"],
  "replacements": [
    {
      "tasks": ["casefold_headers"],
      "conditions": {
        "full": "runs/retry-01/conditions/full",
        "no_context": "runs/retry-01/conditions/no_context",
        "no_feedback": "runs/retry-01/conditions/no_feedback"
      },
      "reason": "Provider outage; replace the affected block after recovery."
    }
  ],
  "baseline_replacements": [
    {
      "tasks": ["casefold_headers"],
      "root": "runs/naive-retry-01",
      "reason": "Baseline episode interrupted before completion."
    }
  ],
  "budget_variable": false
}
```

Paths are relative to the plan. Use your actual condition names and reasons;
declare `budget_variable: true` only for an explicitly controlled budget study.
List Full first in `condition_order` so it remains the paired reference.
All 12 baseline slots and 36 family slots must have complete selected evidence.

```bash
python -m tools.submission assemble \
  --plan recovery.json --output outputs/effective-study

python -m tools.submission package \
  --output dist/hw2-submission.zip --report report.pdf \
  --study outputs/effective-study \
  --supplement original_family=runs/paired-01 \
  --supplement replacement_family=runs/retry-01 \
  --supplement api_check=outputs/api-check-01

python -m tools.submission check dist/hw2-submission.zip
```

Assembly leaves the original runs untouched, retains the original and replacement
histories, and creates a new selection ledger and summaries. It does not convert
partial evidence into completed episodes. Explain the selection rule and both
blocks in the report; choosing replacements by success is not allowed.
The two family supplements above also preserve the original top-level paired
summaries; assembly automatically retains each condition's underlying run directory.
Include only paths that exist for your study.

If you add a top-level source package imported by `run_agent.py`, add
`--include your_package`. It must be a regular Python package containing
`__init__.py`; for a top-level helper module use `--include your_module.py`.
These extensions enter the run's source identity and snapshot, so changing them
invalidates resume. Modules under `harness/` are included automatically.
Keep every required per-task patch, trajectory, metrics,
state, verification, and run identity, plus aggregate evidence and any documented
replacement history. An unsuccessful repair is valid evidence; missing episodes,
fabricated records, and replay results substituted for public-dev API runs are not.

Do not add keys, task repositories, grader tests, generated worktrees or model weights.
The packager automatically includes the required, unchanged course `eval/` runtime;
keep those protected files intact rather than deleting them. The checker validates package structure, protected-file integrity,
required evidence, and manifest consistency. A checker pass supports artifact
integrity; it does not establish a good analysis or a high resolve rate.

For an optional offline audit of saved patches, use a clean trusted release:

```bash
python -m tools.submission reverify dist/hw2-submission.zip \
  --output runs/reverification-01
```

Use a new or empty output directory. This writes fresh public-task verdicts and
`reverification_summary.json` without any model calls or API key. TAs use this
saved-patch workflow when checking the submission.

Grading is implementation/robustness 25, experiment completion/evidence 30,
ablation/failure analysis 30, and reproducibility/budget 15. Staff verify saved
patches and use replay/toy and deterministic checks without your API key or new
network runs. There is no performance bonus, leaderboard, private/final API grading
run, interview, oral checkoff, or live code walkthrough.

`GRADING_POLICY.json` records these categories and the hard caps.

## Action protocol and patch boundary

Native mode registers these six tools individually:

| Action | Required arguments | Optional arguments and defaults |
|---|---|---|
| `list_files` | none | `path: str = "."` |
| `read_file` | `path: str` | `start_line: int = 1`, `end_line: int = 240` |
| `search` | `query: str` | `path: str = "."` |
| `write_file` | `path: str`, `content: str` | none |
| `run_tests` | none | `command: str = "python -m pytest -q"` |
| `finish` | `summary: str` | none |

The normalized/JSON-mode envelope is
`{"action": <str>, "args": <object>, "thought": <str>}`. The starter normalizes
`file_path` to `path`. Native mode accepts exactly one recognized function call
with object arguments; raw calls/final content, normalized action, usage, and
reasoning are recorded separately. Reasoning-only text is not an action.

Unknown/multiple calls, malformed values, escaping paths, protected writes, and
unsupported pytest options produce explicit errors. `run_tests` permits only
worktree-rooted pytest commands. Diagnostic subsets are allowed, but local
`finish` requires the exact task-designated visible command to pass after the
last write. Tests do not inherit model credentials or host pytest plugins/options.

The verifier applies a source patch to a fresh repository and checks all F2P/P2P
tests. Patches changing protected test/runtime/configuration/dependency paths,
changed source symlinks, or patches above 1,000,000 bytes are rejected. Follow
`EVALUATION.md` for the source policy. A local `resolved` metric means visible
completion only; fresh patch verification plus complete, certified budget evidence
determines the reported within-budget result.
