# HW2 Evaluation Contract

## Grading from saved evidence

Staff grade submitted code, the required 12-task baseline and three-condition
12-task family, the report, and reproduction materials. They verify saved patches
on fresh public repositories and use replay/toy and deterministic harness checks.
No student's API key or new model/network run is required for grading.

From a clean, trusted release on a supported isolated host, audit an archive with:

```bash
python -m tools.submission check PATH/TO/hw2-submission.zip
python -m tools.submission reverify PATH/TO/hw2-submission.zip \
  --output grading/reverification-01
```

Use a new or empty output directory. `reverify` runs the trusted public-task
verifier on saved patches and writes per-episode results and
`reverification_summary.json`, including verdict mismatches and zero model calls.
It does not execute a new model experiment or require an API key.

The rubric is implementation/robustness 25, experiment completion/evidence 30,
ablation/failure analysis 30, and reproducibility/budget 15. Raw resolve rate,
baseline improvement, model choice, spending, and class rank do not award points.
Zero or negative improvement can receive full credit. There is no performance
bonus, private-validation submission, leaderboard, final-hidden API grading run,
interview, oral checkoff, or live code walkthrough.

## Safe execution

Generated task code and saved patches must run with the released isolation:
macOS's built-in `sandbox-exec`, or bubblewrap plus libseccomp on Linux/WSL2.
Missing/unsupported isolation is a fail-closed environment error. No GPU or Docker
is needed for the normal student workflow. Follow `README.md` for dependencies.

The API parent holds the credential. Git/test subprocesses do not receive it or
network access. Worktrees and writable scratch are bounded; protected test inputs,
host files, and evaluator state are outside the task's authority. OS resource/time
controls and bounded output supplement source-policy and test-completion checks.
Neither a source allowlist nor sanitized environment alone is a general sandbox.

## Fresh patch verification

For each saved patch, the verifier:

1. starts from the original public task repository and trusted test inputs;
2. checks patch size, protected paths, symlinks, and source policy;
3. applies the patch to a clean repository;
4. runs all F2P tests and P2P regression tests within the shared task timeout;
5. requires complete expected test outcomes, not merely process exit zero.

Public toy/dev acceptance tests are available for local verification but never
copied into agent worktrees or sent to a model. A nonempty patch and the exact
visible command passing after the last write set the agent's local `resolved`
status; this is not fresh-verifier success.

The patch-only verifier (`eval/validate_patch.py`) reports:

```json
{
  "resolved": true,
  "patch_valid": true,
  "f2p_passed": true,
  "p2p_passed": true,
  "protected_paths_clean": true,
  "details": {}
}
```

The experiment's **resolved within budget** result additionally requires complete
run evidence, certified usage, and compliance with recorded caps and hard limits:
12 HTTP attempts including retries, 120,000 provider-reported tokens, and 1800 active
seconds including API latency, pacing, and retry waits. Naive's configured attempt
cap is six. Fresh verification time is separate from the agent episode.

Reaching a cap exactly is allowed. A `budget_exhausted` termination does not itself
imply that a saved patch fails tests. A passing patch that exceeds a cap or has
unknown usage is not certified resolved within budget. Report patch outcome,
termination, infrastructure status, and budget certification separately.

## Experiment records and interruptions

Save the unmodified starter baseline before editing:

```bash
python -m eval.run_dev --output_root runs/naive-baseline --backend endpoint --seed 2026 \
  --config config/examples/six_calls.json
```

Then use `eval.run_paired` with Full and two single-factor variants. The README
provides a context/feedback example; at least one contrast must ablate your own
implemented mechanism. Other component studies are allowed. Use the same provider/model/generation/action/
reasoning settings and ordered tasks throughout the main study. Local seeds do
not guarantee deterministic API responses.

Retain all per-task patch, trajectory, metrics, state, verification, identity, API,
and error artifacts and aggregate JSON/CSV. Distinguish functional failures,
regressions, source-policy rejections, action errors, budget exhaustion, provider
errors, verifier infrastructure errors, and incomplete work. Missing usage is
uncertified; it must not be replaced by zero or an invented tokenizer count.

A complete failed repair remains experimental evidence. A complete provider-error
record demonstrates the attempt but does not establish a functional result or
certified token cap. An interrupted record is not a completed episode; preserve it
and its recovery explanation. Do not silently omit failures, retry-drop attempts,
or overwrite evidence. A documented provider outage can be replaced only under
the handout's affected-paired-block rule, preserving originals and replacements.

Budget caps are safety limits, not equivalent compute across providers. Keep caps
equal in controlled experiments unless budget itself is declared as a variable;
state the budget intervention and retain all other controls.

## Source policy for repository repair patches

These task repositories implement bounded Python algorithms. Patches may import
local Python modules and the following standard-library roots:

```text
abc base64 binascii bisect calendar codecs collections copy csv dataclasses datetime
decimal email enum fnmatch fractions functools hashlib heapq io ipaddress itertools
json math numbers operator os pathlib re shlex statistics string time typing
unicodedata urllib
```

Additional member restrictions apply: `os` is limited to `path`, `name`, `sep`,
`altsep`, `extsep`, `linesep`, and `fspath`; `io` to `StringIO` and `BytesIO`;
`urllib` to `parse`. The directive `from __future__ import annotations` is allowed;
other `__future__` imports and aliases are rejected.

The policy rejects testing/runtime introspection imports, dynamic loading,
`eval`, `exec`, `compile`, reflective builtins (`globals`, `locals`, `vars`,
`getattr`, `setattr`, `delattr`), raw file/descriptor access, native/bytecode
additions, mutation of imported runtime objects, and sensitive frame/loader/code/
closure/module attributes. It checks every repository Python module, including
new helpers. See [the verifier source](eval/validate_patch.py) for exact checks.

Tests, evaluator/configuration/dependency files, runtime-injection files such as
`sitecustomize.py`, changed source symlinks, and patches above 1,000,000 bytes are
rejected. The policy applies to the generated **repository patch**. The separate
student-owned harness may implement the allowed local tools, file operations,
and static analysis while preserving isolation.

The verifier freezes expected test identities from trusted original inputs before
applying the patch. The repaired run must execute the expected parametrized tests
and complete setup/call/teardown. Early exit, missing reports, skipped tests, or
incomplete execution cannot establish success. Infrastructure failures must remain
explicit and must not be converted into a model-quality outcome.
