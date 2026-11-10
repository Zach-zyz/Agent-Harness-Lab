# HW2 Technical Report Working Draft

Replace this guidance with a 4-5 page report, excluding references and appendix.
PDF or Markdown is accepted; keep Markdown's rendered length comparable. Report
what happened, including null/negative effects and provider errors. Raw resolve
rate and improvement are not grading thresholds.

## 1. System design

Explain the architecture, state, tool interfaces, localization/context strategy,
testing, error recovery, patch construction, and stopping. Include a compact
architecture diagram and identify your changes to the student-owned starter.
Identify at least one substantive mechanism you implemented, its source locations,
and a deterministic behavior check. Distinguish your contribution from inherited
starter behavior and from merely changing the model or configuration values.
Explain how protected client/budget/isolation boundaries remain intact.

## 2. Baseline and paired ablations

Identify the unchanged starter and `config/examples/six_calls.json` baseline on
all 12 public tasks. Distinguish it from the separate 36-episode primary family:
exactly Full and two single-factor variants (for example, no-context and
no-feedback). At least one contrast ablates your own mechanism. For each, give a
hypothesis, the exact single factor changed, and evidence that the other settings
were held fixed.

Record provider, base URL, requested/returned model identity, action mode, reasoning
effort, temperature, top-p, output cap, local seed, and all episode budgets. Full
and its two variants normally use 12 attempts / 120,000 tokens / 1800 active seconds;
the Naive reference uses six attempts. Do not claim a baseline-to-full difference
isolates feedback. Do not equate budgets across providers with equal compute.

Include or reference the full 12-by-four task matrix. Show patch/F2P/P2P status,
within-budget certification, error class, attempts/retries, reported tokens,
unknown usage, and elapsed time. Show paired wins/losses/ties and resource changes.
Give intervals or explicitly explain the limited inference possible from 12 tasks
and stochastic API outputs. Explain any provider-outage replacement block and
retain/report both original and replacement attempts without selection by outcome.
Label extra conditions as supplementary exploratory evidence, retain them with
`--supplement NAME=PATH`, and keep their results outside the primary matrix/aggregates.

## 3. Four evidence-backed case studies

For each failure case, name the task, condition, episode path, and trajectory event
or line/test evidence. Locate the first consequential event, classify the failure,
explain the mechanism, and give a concrete prevention/recovery step and a way to
check it. Distinguish provider/infrastructure errors from functional failures.

Cases may come from different conditions, including distinct trajectories on the
same task. If there are fewer than four failures, discuss all of them and use
trajectory-backed near-misses or limitations for the remaining cases. Mark these
as risks/limitations, not observed failures. Never manufacture a failure.

## 4. Budget profile

Report mean, median, and maximum HTTP attempts, provider-reported tokens, active
elapsed time, test invocations/durations, and tool use by condition. Include retries,
API/pacing/backoff waits, unknown-usage counts, and separately timed verification.
Explain configured versus actual caps, over-budget/uncertified outcomes, and any
planning reservations. Unknown usage or price is not zero. Record actual/estimated
monetary cost and its source if available; do not claim a guaranteed free quota.
Distinguish provider-reported totals from protected-client charges, including any
conservative reasoning addition disclosed in `accounting_notes`; safety-cap
accounting is not a provider bill.

## 5. Generalization, limitations, and next steps

State which hypotheses the evidence supports, contradicts, or leaves unresolved.
Discuss public-task overfitting, API/provider variability and unexposed versions,
and why a local seed cannot guarantee identical outputs. Explain what the 12 tasks
do not establish and propose concrete follow-up checks. Zero or negative improvement
can earn full credit with correct work and supported analysis.

## Appendix and artifact map

Map every table and case citation to submitted paths. Put extended task tables,
config diffs, prompts/tool schemas, and trajectory excerpts here. Keep API keys and
private task material out of the report. Complete `REPRODUCE.md` separately.
