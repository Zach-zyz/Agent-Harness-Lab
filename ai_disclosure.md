# AI Assistance Disclosure

## AI tools used for this assignment

I used **Claude Code** (Anthropic, Claude model, in the VS Code extension) as an assistant throughout the project. I ran every command and change every code myself. The assistant gave instructions and suggestions, and it read files and run outputs only to check my work.

It helped with the following:

- **Understanding the project:** explaining the assignment, the harness
  architecture, the budget and finish-gate rules, fresh verification (F2P/P2P), and what the baseline results meant.
- **Verification:** it helped me check the code in `harness/localize.py`, `harness/agent.py`, `run_agent.py`, and help me write test code in `harness/check_localize.py`. I changed the code myself and reviewed the diffs.
- **Analysis and writing:** it computed summary statistics from the saved run artifacts (resolve rates, paired wins/losses/ties, budget tables, the 48-episode matrix) and drafted the report, `REPRODUCE.md`, and this disclosure. I revised the report's wording and structure myself and decided the final content.

I also asked a separate AI agent to review a report draft. Its feedback led to several corrections: two-call episode counts, separating model-event lines from tool-event lines, labeling the baseline source-policy rejection, and moving the full matrix to the appendix. Each correction was checked against the logs before it was
applied.

## What I verified myself

- **Tests and checks:** I ran `python -m pytest -q` (568 passed),
  `python -m harness.check_localize`, and `python -m eval.run_toy` after each code
  change.
- **Experiments:** I ran all endpoint checks, smoke runs, the baseline, and the
  36-episode paired experiment. I did not rerun or replace any episode to change the
  results.
- **Diffs:** I reviewed the git diffs of my changes and confirmed that no protected
  file (`harness/budget.py`, `harness/model_client.py`, `harness/sandbox.py`,
  `eval/`, task tests, `requirements.lock`, `config/examples/six_calls.json`) was
  modified.
- **Report evidence:** I checked the numbers, paths, and trajectory line numbers
  cited in the report against the saved artifacts.

No API key or credential was shared with the AI assistant or written into any file.

## Runtime model used in the experiments

The required experiments used **`openai/gpt-oss-20b`** through **OpenRouter**
(`https://openrouter.ai/api/v1`), with native function-tool action mode and
reasoning effort `low`. Detailed settings are in `REPRODUCE.md`.
