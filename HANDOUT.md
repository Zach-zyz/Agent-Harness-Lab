# Assignment 2: Agent Harness Challenge - Code Repair

**Course**: CSE 8803 - Language Models and Language Agents (Fall 2026, Georgia Tech)  
**Instructor**: Prof. Chao Zhang  
**Released**: Thursday, October 1, 2026  
**Final due date**: Monday, November 16, 2026, 11:59 PM ET  
**Weight**: 15% of the final course grade  
**Format**: Individual

> This file is the authoritative HW2 handout, distributed as `HANDOUT.md` in the
> student release. If a stale copy appears elsewhere, this specification governs.

## 1. Learning Goals

Design and evaluate the software around a language model: the agent loop, prompts,
state, tool interfaces, localization, context management, testing, recovery, patch
construction, and budget-aware stopping.

Implement at least one substantive harness mechanism beyond selecting a model,
changing numeric settings, or toggling the supplied switches. Examples include a
new localization/context strategy, structured prompt/state representation, tool
design, recovery policy, or budget-aware stopping rule. Explain its source changes
and test its behavior. Code length and positive performance gains are not requirements.

You choose a compatible hosted API and hold the provider, model, and generation
settings fixed across your main baseline and ablation family. The starter defaults
to NVIDIA at `https://integrate.api.nvidia.com/v1`, model `openai/gpt-oss-20b`.
Use your own account and `HW2_MODEL_API_KEY`. Other compatible HTTPS providers and
models are allowed through `HW2_MODEL_BASE_URL` and `HW2_MODEL_ID`. `API_SETUP.md`
documents the loopback-only HTTP exception for optional local testing.

No GPU, model download, self-hosted server, or Docker is required for the normal
student workflow. The course does not distribute shared API keys or provide API
credits. Start with the CPU replay lab, check your account's current access and
pricing, and run one public task before starting a batch. Report access problems
to staff early.

The grade measures implementation, complete and verifiable experiments, analysis,
and reproducibility. **A correct experiment with zero improvement can earn full
credit.** Neither raw resolve rate nor improvement over a baseline determines
points, and there is no performance bonus.

## 2. Task and Resolution

For each task, the harness receives a clean Python repository, an issue, environment
metadata, and a designated visible-test command. It must produce a source patch.
Acceptance tests remain outside the agent worktree and must not be sent to the
model, even though public development tests are available for local verification.

A patch passes fresh verification only if:

1. it applies to a clean copy of the base repository;
2. all required `FAIL_TO_PASS` (F2P) and `PASS_TO_PASS` (P2P) tests execute and pass;
3. it changes only allowed source files and passes the published source policy;
4. verification finishes within the task's test timeout with complete test results.

Skipped tests, early exits, and successful process exit without complete tests do
not establish success. A visible-test pass alone is not a fresh-verifier pass.

Report an episode as **resolved within budget** only when its patch passes fresh
verification, its evidence is complete, and its recorded HTTP-attempt, token, and
time use is certified within both its configured caps and the assignment limits.
A complete failed repair is valid experimental evidence. A passing patch with an
over-budget or uncertified episode must be labeled accordingly.

Separate functional failures, regressions, source-policy rejections, action/parse
errors, budget exhaustion, provider/transport failures, verifier infrastructure
errors, and incomplete runs. An API error is not evidence that a repair failed
functionally. Missing provider usage makes token compliance **uncertified**, not
zero-cost. Record these distinctions in the task matrix and report.

## 3. Release Contents and Setup

The release includes:

- two deterministic replay tutorials and 12 public development tasks;
- `run_agent.py`, student-owned harness modules, and protected budget/model-client code;
- `eval.run_toy`, `eval.check_endpoint`, `eval.run_dev`, `eval.run_paired`, and a fresh verifier;
- submission packaging/checking tools, configuration examples, and report templates;
- `HARNESS_LAB.md`, `API_SETUP.md`, `MODEL_CARD.md`, `BASELINES.md`, and `EVALUATION.md`.

`GRADING_POLICY.json` records the same 100-point categories and hard caps in
machine-readable form.

Use Python 3.11+ and Git on macOS, Linux, or Linux under WSL2. Native Windows
Command Prompt and PowerShell are not supported. Generated-code execution requires
macOS's built-in `sandbox-exec`, or bubblewrap plus libseccomp on Linux/WSL2.
The runtime fails closed if supported isolation is unavailable. Follow `README.md`
before running tests; source-policy checks alone do not isolate code from the host.

`HARNESS_LAB.md` is the executable prerequisite, available on October 1. Allow
60-90 minutes for the hands-on tutorial and launching experiments, plus API account
setup and batch time. The lab has no separate submission. `MODEL_CARD.md` records
API defaults, compatibility requirements, and reproducibility limits.

## 4. Experimental Data

| Split | Size | Use |
|---|---:|---|
| Toy | 2 | Offline replay, debugging, and deterministic harness checks |
| Public dev | 12 | Required baseline, paired experiments, and failure analysis |

All required model experiments use the 12 public development tasks. There is no
private-validation submission, leaderboard, or final-hidden API run in the current
student contract. Staff retain a private task bank for possible future diagnostics;
it is not an additional grading split for this assignment.

Public acceptance tests support local verification but are not agent inputs.
Do not hardcode task solutions, task IDs, reference patches, or test answers into
the harness.

## 5. API and Action Contract

The starter uses native function calling by default and registers six actual
tools: `list_files`, `read_file`, `search`, `write_file`, `run_tests`, and `finish`.
It accepts exactly one recognized action per model step, validates arguments,
and records the raw response and normalized action separately. Unknown tools,
multiple tool calls, and non-object arguments are errors.

For a compatible provider that needs the content-based protocol, set
`HW2_MODEL_ACTION_MODE=json`. The action object is:

```json
{"action": "read_file", "args": {"path": "pkg/example.py"}, "thought": "Inspect the source."}
```

Valid final-content JSON may be normalized by the starter adapter. Reasoning text
is never executed as an action. NVIDIA's default uses `reasoning_effort=low`;
keep action mode and reasoning settings fixed in the required comparisons.
The exact argument schemas are in the release README.

Use one designated provider/model per main experiment. Hold temperature, top-p,
output limit, action mode, reasoning effort, and other generation settings fixed
across the saved baseline and required family. The provided configs use temperature
0.2, top-p 1.0, and maximum output 4,096. Set up provider compatibility before saving
the baseline. Separate exploratory model/settings comparisons are allowed, but do
not substitute them for the required controlled harness study. If the main
provider/model/settings change, establish a new complete main baseline and family
and retain the earlier attempts.

HTTP client calls must run serially in the main thread. The protected client's
total request deadline uses `SIGALRM`; worker-thread calls and conflicting process
alarms are unsupported. The local experiment seed is recorded but is not sent
in API requests.

Only the harness parent process may use the API credential. Do not place keys in
source, configuration files, reports, trajectories, submissions, or test processes.
The runtime agent may contact only its designated model API; web search, auxiliary
models/embeddings, and remote execution services are outside this assignment.

## 6. Episode Budgets

| Resource | Hard maximum per episode |
|---|---:|
| HTTP attempts to the model, including every retry | 12 |
| Tokens under protected-client accounting | 120,000 |
| Active elapsed episode time | 1800 seconds |

The immutable Naive configuration lowers the attempt cap to six. The default
three-condition family uses 12 attempts per episode. Configuration field
`max_model_calls` means **HTTP attempts**, including failed requests and retries;
a logical model step with two retries consumes three attempts.

Token accounting includes all usage the provider reports for prompts, history,
tool schemas/observations, completions, and reasoning. Preserve raw usage,
normalized counters, and `accounting_notes`. The protected client treats ordinary
nested reasoning counts as part of completion usage. For an ambiguous top-level
reasoning count, or a nested count larger than completion usage, it conservatively
charges at least prompt + completion + reasoning, retaining any larger reported
total. This safety-cap accounting may exceed the provider's reported total; it is
not a billing calculation. Do not estimate usage from visible answer length or
drop usage returned with an error.

Missing or invalid usage must be marked unknown and the episode's token cap
uncertified. The client records a reservation and rejects an otherwise successful
response without valid usage; the default agent stops that episode without
executing the returned action. See `API_SETUP.md` for error/retry handling.
A reservation for unknown usage is not a measured bill or certified token bound.

Active elapsed time includes API latency, rate-limit pacing, retry/backoff waits,
harness computation, tools, visible tests, and patch construction. Account setup,
dependency installation, and environment startup before an episode are outside its
timer. Fresh patch verification is reported separately. Do not subtract provider
waits to claim compliance. Reaching a cap exactly is allowed; exceeding it is not.
Keep the actual termination reason, even when a patch passes after the last
allowed action and the episode ends with `budget_exhausted`.

Budgets are safety caps, **not equivalent compute across models or providers**.
Tokenizers, reasoning accounting, latency, and pricing differ. Keep identical
caps within controlled comparisons unless budget itself is an explicitly declared
experimental variable. Declare a budget intervention with
`eval.run_paired --budget-variable`; hold all other factors fixed for that contrast. Equal family
caps may be lower than the published hard caps. At least one required contrast
must still ablate your implemented harness mechanism. Report tool calls, visible-test counts/durations,
and verifier time, although raw tool-call count has no separate hard cap.

### Planning and interruptions

The minimum experiment is **12 baseline + 3 × 12 family = 48 episodes**.
The six-attempt baseline plus three twelve-attempt conditions permits at most
504 HTTP attempts, 5.76 million tokens, and 24 cumulative active hours for this
minimum set. These are upper bounds, not expected consumption or free allowances;
probes, extra studies, and documented replacement blocks add usage.

One staff account displayed **up to 40 requests/minute** during the September 28
pilot. No daily cap or class-wide free quota was verified. Check your own account's
terms and limits; the course does not promise free access through the deadline.

Preserve all attempts and errors. Never discard failed repairs or retries from
the denominator, overwrite a failure with a success, or retry until a result looks
better. Resume may reuse only complete evidence with matching identities and hashes;
an interrupted partial run must remain available as incomplete evidence.

For a documented provider outage, you may rerun the affected paired block in a
new output location after service recovers. A paired block means the same affected
task(s) in **all conditions of the family**; for the independent baseline, rerun
the corresponding affected baseline slot(s). Preserve original and replacement
attempts, timestamps, errors, identities, and the reason for replacement. Declare
which complete block is used in the main table and report both. Do not select
replacements based on repair outcomes. Authentic, fully recorded provider failures
remain evidence of attempted work but cannot establish functional outcomes or
certified token compliance. Report persistent access problems to staff.

The release README provides the recovery commands: rerun affected tasks under new
roots with `--task`, then use `tools.submission assemble --plan recovery.json`
and `package --study` to retain both histories and create a separate selection
ledger. Attach probes, smoke runs, or other partial records with
`package --supplement NAME=PATH`. Supplemental records do not inflate coverage
or enter the main aggregates. Never edit old receipts to make a run appear complete.

## 7. Implementation Rules and Interface

You may edit `run_agent.py`, `harness/agent.py`, `harness/tools.py`,
`harness/localize.py`, `harness/state.py`, `harness/patch.py`, prompts, and your
experiment configurations. You may add source modules or tool schemas while
preserving the model-client interface, budget boundary, safe execution rules,
CLI, and parseable evidence. Use standard libraries and released dependencies.

Do not modify the sandbox controls, `harness/budget.py`, `harness/model_client.py`,
`eval/`, task tests, `requirements.lock`, or the provided
`config/examples/six_calls.json`. Request staff help for a compatibility or
dependency issue instead of changing protected files.

Do not fine-tune weights, add another pretrained model, ship precomputed repairs,
modify grader behavior, or access services other than the designated model API.
Repository repair patches must obey the source/import policy in `EVALUATION.md`.
These patch restrictions differ from the permitted file/tool operations in the
separate harness.

The required interface is:

```bash
python run_agent.py \
  --task_dir <path> \
  --output_dir <path> \
  --seed <int>
```

Each episode writes:

```text
output_dir/
  patch.diff
  trajectory.jsonl
  metrics.json
  final_state.json
```

The batch runner also saves `verification.json` and aggregate results. Preserve
every additional API-attempt, run-identity, receipt, or error artifact emitted by
the released runner. Trajectories must expose requests/responses, normalized
actions, tool observations, errors, budget changes, and state transitions, with
secrets redacted.

Metrics must distinguish visible status from fresh verification, count every
HTTP attempt and retry, retain raw provider usage and unknown-usage status, and
record elapsed time including waits. Also record tool/test use, termination,
provider/model/settings, task/config/runtime identity, and local seed.
Deterministic task ordering and a recorded seed do not guarantee deterministic
API output.

When editing the agent loop, preserve accounting ownership: the API client charges
response usage and returns `ModelResponse.usage_charged=True`; do not charge it
again. The agent charges replay response tokens when `usage_charged=False`.

`run_tests` accepts worktree-rooted pytest commands only. Diagnostic subsets are
allowed, but local `finish` requires the exact designated `visible_command` to
pass after the most recent write. Tests cannot select alternate configurations,
load host plugins, read grader tests, or inherit API credentials.

## 8. Required Experiments

### 8.1 Immutable baseline: 12 episodes

Before editing the starter, run all 12 public tasks with
`config/examples/six_calls.json` using the release README command. Preserve the
unmodified release and checksum, exact configuration, run identities, patches,
logs, and verification. The Naive baseline has at most six HTTP attempts, no
bootstrap context, and no automatic test feedback. This reference is separate
from the three-condition family and must not be overwritten by later edits.

### 8.2 Primary paired family: 36 episodes

Declare exactly three primary conditions: Full and two single-factor variants.
Run them on the same ordered 12 tasks and declared local seed, for 36 family slots
in addition to the 12 independent baseline slots. Use the same
provider/model/generation settings as the baseline and identical budgets within
the family, normally 12 attempts / 120,000 tokens / 1800 seconds.

Additional conditions are allowed as supplementary exploratory evidence. Preserve
their runs and attach them with `package --supplement NAME=PATH`; they do not
replace required conditions or enter primary coverage/aggregates. The release
README explains how to retain a run that already contains extra conditions.

At least one contrast must disable or replace your own substantive mechanism.
The second may study another component, including one supplied by the starter.
Choose two meaningful hypotheses and explain the exact intervention. The following
provided configurations are a worked example for a context/feedback study:

| Condition/config | `bootstrap_context` | `auto_test_after_write` | Attempt cap |
|---|---|---|---:|
| Full: `config/default.json` | true | true | 12 |
| No context: `config/examples/no_context.json` | false | true | 12 |
| No feedback: `config/examples/no_feedback.json` | true | false | 12 |

Each variant changes one factor relative to Full. Keep all other code,
configuration, and settings identical. You may use these switches to ablate a new
context/feedback mechanism you implement, or provide configurations for other
components such as localization, memory or recovery. Name the mapping clearly and
keep the family configurations synchronized when developing your harness.

The separate six-attempt baseline is not a same-budget control for the
twelve-attempt family. Report that difference explicitly; do not attribute a
baseline-to-full difference solely to feedback.

For all conditions, submit a per-task outcome matrix with failure/error classes,
F2P/P2P results, attempts, usage certification, tokens, and elapsed time. Show
paired wins/losses/ties and resource changes. Report uncertainty intervals or
explicitly explain why 12 tasks cannot establish small differences. Do not claim
that a one-task improvement proves a component works or that API seeds remove
sampling/provider variability.

### 8.3 Failure analysis

Analyze at least four evidence-backed failure cases **across any of the required
conditions**. A case identifies a task, condition, and episode; multiple conditions
on the same task may support distinct cases if their trajectories justify distinct
diagnoses. Do not split one event into several nominal cases.

For each case, cite the trajectory event/test evidence, locate the first
consequential failure, explain the mechanism, and propose a specific prevention
or recovery step. Distinguish functional mistakes from localization, context,
action parsing, policy rejection, regression, budget, provider, and infrastructure
problems.

If fewer than four failures occur, discuss **all** failures and fill the remaining
case studies with evidence-backed near-misses or limitations, including trajectory
citations. Explain the risk without presenting an imagined failure as observed.
If there are no failures, four such near-miss/limitation studies satisfy this
requirement. Never manufacture failures or rerun successful work to seek failures.

## 9. Submission and Report

Submit one checker-validated `hw2-submission.zip` through Gradescope. Use the
release README packaging and independent checking commands. Include:

- student-owned source and exact experiment configurations;
- the required baseline and family evidence, including unsuccessful episodes;
- a completed `REPRODUCE.md` and `ai_disclosure.md`;
- a 4-5 page technical report, excluding references and appendix.

The report may be PDF or Markdown; for Markdown, keep the rendered report to the
same page-length target. Organize it as:

1. **System design:** architecture, state, tools, control flow, and stopping.
2. **Baseline and paired ablations:** conditions, hypotheses, controls, complete
   per-task outcomes, uncertainty, and resource changes.
3. **Failure analysis:** the four case studies or the fewer-failures alternative.
4. **Budget profile:** mean, median, and maximum attempts, reported tokens, elapsed
   time, test/tool use; retries, unknown usage, and documented reruns.
5. **Generalization and limitations:** what the 12 public tasks and this provider
   establish, what remains uncertain, and concrete next steps.

The appendix may hold prompts, schemas, complete tables, configs, and trajectory
excerpts. Do not upload task repositories, grader tests, model weights, or keys.
Include honest pricing/cost records where available; unknown cost is not zero.

## 10. Grading

| Component | Points | What is assessed |
|---|---:|---|
| Implementation and robustness | 25 | Correct loop, tools, state/feedback, recovery, safety |
| Experiment completion and verifiable evidence | 30 | 12 + 36 episode coverage, authentic artifacts, reconciled outcomes |
| Ablation and failure analysis | 30 | Controlled comparisons, supported interpretation, four case studies |
| Reproducibility and budget discipline | 15 | Provenance, commands, honest accounting, usable package |
| **Total** | **100** | |

Implementation points are divided equally among student mechanism/integration, tools,
state/feedback, recovery/budget behavior, and safe patch handling (5 each).
Experiment points cover baseline coverage (6), family coverage (12), artifact
reconciliation (6), and complete attempt/outcome accounting (6).
Analysis points cover controls (8), paired interpretation (6), case studies (12),
and supported conclusions (4). Reproducibility points cover provenance (5),
commands (4), budget/cost reporting (4), and packaging/disclosure (2).

TAs verify saved patches on fresh public repositories, inspect code and reports,
and use replay/toy and deterministic harness checks. **Students' API keys and new
network/model runs are not required for grading.** Saved responses support
inspection of model behavior; they are not a promise that another API run will
return the same text.

No points are assigned by raw resolve rate, baseline delta, model prestige,
spending, class rank, or a hidden performance threshold. Zero or negative measured
improvement can receive full credit when the implementation, evidence, and
analysis meet the criteria. A documented provider failure is evaluated for honest
evidence and handling, not silently converted to a functional repair failure.

**There is no interview, oral checkoff, or live code walkthrough.**

## 11. Timeline

| Date | Milestone |
|---|---|
| Oct 1 | Release, harness lab, toy/public tasks, API setup guide |
| Oct 8 | Recommended: offline lab and one real public task completed |
| Oct 13 | Recommended: immutable 12-task baseline saved |
| Oct 20 | Recommended: three-condition paired family completed |
| Oct 27 | In-class design clinic; review evidence and failure analysis |
| Nov 5 | Final harness, report, and reproducibility package due |

## 12. Academic Integrity and Background

This is an individual assignment. Discuss papers and high-level ideas, but do
not share code, prompts, schemas, configurations, patches, or trajectories.
AI assistants may help with brainstorming, coding, debugging, and writing;
disclose that help and substantiate the submitted design through your own checks,
code, traces, and experiments. At runtime, use only your declared model API under
the rules above.

Core readings:

- [Yao et al., ReAct](https://arxiv.org/abs/2210.03629)
- [Yang et al., SWE-agent](https://arxiv.org/abs/2405.15793)
- [Wang et al., CodeAct](https://arxiv.org/abs/2402.01030)
- [Jimenez et al., SWE-bench](https://arxiv.org/abs/2310.06770)

## 13. FAQ

**Do I need a GPU or a course key?**  
No GPU is required. Use your own hosted-provider account and key. No shared course
key or credits are supplied.

**Can I choose another model?**  
Yes, through a compatible HTTPS provider. Fix provider/model/settings before the
main baseline and keep them fixed throughout the required family.

**Must I match the staff pilot's 9/12 or 10/12?**  
No. Those observations are illustrative, use different call budgets, and are not
grading targets. See `BASELINES.md`.

**What if every patch succeeds, or my ablations do not improve anything?**  
Full credit remains possible. Analyze supported near-misses/limitations when there
are fewer than four failures, and report null or negative differences honestly.

**Can I omit an API error and run that task again?**  
No. Preserve all attempts. A documented outage permits the paired-block replacement
procedure in Section 6, with both blocks retained and no selection by outcome.

**Does seed 2026 reproduce the API output exactly?**  
No. It records the local experimental seed and is not sent in API requests.
Providers may change serving behavior. Reproducibility rests on saved evidence,
identities, configurations, and deterministic local checks.

---

*Last updated: November 7, 2026*
