# HW2 Reproduction Record

Replace this template with the exact commands and identities for your submission.
Do not include API keys or require a grader to obtain one. Grading uses saved
patches/evidence and deterministic offline checks; new API output need not match.

## Environment and provenance

Record the starter archive SHA-256, release version, clean-baseline source/config
identity, submitted runtime/source identity, task/config hashes, Python/dependency
versions, OS version, and isolation backend. State how macOS `sandbox-exec` or
Linux/WSL2 bubblewrap plus libseccomp was checked. No GPU record is required.

Disclose runtime changes between episodes or resumed batches, with dates and
affected runs. Python patch/binary and OS-version updates are allowed when Python
major.minor, implementation, OS family, machine architecture, and dependency
versions still match. Source/config/task/model/settings checks remain strict.
Preserve each episode's full original runtime metadata and identities; do not
rewrite saved records to match a newer environment.

Record provider/base URL, requested and returned model IDs, any exposed revision
or fingerprint (or unavailable), action mode, reasoning effort, temperature,
top-p, maximum output, local seed, and timestamps. State that the released client
records the seed locally but does not send it in API requests; do not assert
deterministic outputs.

## Commands and artifact locations

Supply commands from a clean extracted release for:

1. installing dependencies and verifying supported isolation;
2. running unit checks, `python -m eval.run_toy`, and saved-patch
   `python -m tools.submission reverify ARCHIVE --output NEW_DIRECTORY` without an API key;
3. configuring the API using variable names only and running the endpoint check,
   with retained probe evidence under `outputs/api-check-TIMESTAMP/` or the new
   path supplied with `--output-dir` (include failed/interrupted probes);
4. running the immutable 12-task `six_calls.json` baseline;
5. running exactly three primary conditions, Full and two declared single-factor
   variants, on the same 12 tasks,
   including a contrast that ablates the implemented student mechanism;
6. packaging and independently checking `hw2-submission.zip`.

Separate the offline inspection/check steps from optional repetition of API
experiments, which would require the runner's own credential and may consume
quota. Use the existing commands from `README.md` with your actual paths.
Include extra source packages via `--include` when applicable.
Attach retained probes, smoke runs, extra exploratory conditions, and incomplete
histories with the package command's `--supplement NAME=PATH`. These records are
outside the main coverage and aggregates. If recovering a partial study, include
the exact `recovery.json` plan, `tools.submission assemble` command, resulting selection ledger, and
`package --study` command shown in `README.md`. Assembly retains both original
and replacement evidence; do not manually rewrite old summaries or receipts.

Map the baseline, all family conditions, per-task artifacts, and report tables to
paths. Explain how the original unmodified starter was preserved separately from
your current harness. List exact configs and their single-factor differences.

## Attempts, budgets, and interruptions

Record configured caps and how attempts (including retries), reported tokens,
unknown usage, and 1800-second active elapsed time including API waits were counted.
Keep raw provider usage and `accounting_notes`; distinguish conservative reasoning
charges and unknown-usage reservations from measured/billed tokens. Describe
source pricing and actual/estimated cost where available, or state unavailable.

List every additional attempt, interrupted run, and provider-outage replacement,
with original/replacement paths, timestamps, reason, affected task block, and the
outcome-independent selection rule for the main table. Preserve both blocks and
explain which evidence remains incomplete or uncertified. Never remove a failed
repair or retry from the record to improve reported results.
