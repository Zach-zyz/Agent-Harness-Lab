# HW2 Baseline and Controlled Experiments

## Required student reference

Before modifying the starter, run `config/examples/six_calls.json` on all 12 public
development tasks. Save the original release/checksum, config, complete run
artifacts, and fresh-verifier results. This immutable **Naive** reference uses
six HTTP attempts, no bootstrap context, and no automatic test feedback.

The separate primary family contains exactly three conditions: Full and two
single-factor variants on the same ordered tasks. At least one contrast must
ablate a substantive mechanism you implement.
The supplied context/feedback configurations are one worked example:

| Condition | Config | Bootstrap context | Automatic test feedback | Attempt cap |
|---|---|---|---|---:|
| Full | `config/default.json` | on | on | 12 |
| No context | `config/examples/no_context.json` | off | on | 12 |
| No feedback | `config/examples/no_feedback.json` | on | off | 12 |

All four configs use temperature 0.2, top-p 1.0, maximum output 2,048, 120,000 tokens,
and 480 active seconds. Fix provider/model/action mode/reasoning settings across
your main baseline and family. Each ablation changes only its named knob relative
to Full; keep other code and settings equal. The default NVIDIA model is
`openai/gpt-oss-20b`, native function calling, reasoning effort `low`.

The minimum is **12 + 3 × 12 = 48 episodes**. A failed repair is still required
experimental evidence. Preserve every attempt, including retries and errors.
Separate provider/transport and verifier infrastructure errors from functional
failures. Missing usage is uncertified, not zero-cost. Follow the handout's
paired-block procedure for documented provider outages; never select retries by
repair outcome.

Retain additional conditions as supplementary exploratory evidence with
`package --supplement NAME=PATH`. They do not replace any of the primary 12+36
slots or enter primary aggregates; see `README.md` for assembly of an existing
run with extra conditions.

## Illustrative staff API pilot: September 28, 2026

The pilot used NVIDIA at `https://integrate.api.nvidia.com/v1`, model
`openai/gpt-oss-20b`, native function calling, low reasoning effort, temperature
0.2, top-p 1.0, and maximum output 2,048. It ran the same 12 public tasks once per
configuration and retained all episode evidence.

| Pilot configuration | Resolved | HTTP attempts | Provider-reported tokens | Cumulative active time |
|---|---:|---:|---:|---:|
| Naive, six-attempt cap | 9/12 | 72 | 102,550 | 16.24 min |
| Feedback, twelve-attempt cap | 10/12 | 52 | 67,199 | 9.19 min |

These are observations under the pilot runtime and verifier. They are **not grade
thresholds**, expected student scores, or a new measurement of every released
configuration. The pilot conditions had different attempt caps and harness
behavior, so the difference cannot be attributed solely to feedback. One run on
12 small public tasks cannot establish a small causal effect, deterministic
outputs, generalization, or class-wide service capacity.

There were no recorded 429/5xx responses, retries, missing usage records, or
over-eight-minute episodes in these two batches. This does not guarantee the same
conditions for students. One account displayed up to 40 requests/minute; no daily
cap or class-wide free quota was verified. The API did not separately report
reasoning-token usage, which is not evidence that reasoning used zero tokens.

The six-attempt pilot failed on `json_pointer`, `safe_member_path`, and
`toposort_stable`; the feedback pilot failed on `csv_records` and
`toposort_stable`. These illustrate that failures can move across conditions and
that a regression test pass does not establish full task correctness. The
`json_pointer` diagnosis included a source-policy issue and a separate functional
bug; later diagnostic checks did not revise the original pilot score.

## How to use these observations

Report per-task outcomes and paired wins/losses/ties, plus attempts, tokens,
unknown-usage status, elapsed time, and tests/tool use. Discuss confounders and
uncertainty. Analyze at least four failure cases across conditions, or all failures
plus enough evidence-backed near-misses/limitations to make four case studies.
Every case needs trajectory evidence; never manufacture a failure.

There is no performance bonus, leaderboard, or private/final API grading run in
the current contract. The grade is implementation 25, experiment completion and
verifiable evidence 30, ablation/failure analysis 30, and reproducibility/budget 15.
Correct work with zero or negative improvement can earn full points. Replay success
checks local control flow and does not replace any required public API episode.
