# Agent evaluation

The unit tests in `tests/` check the tracker. These evaluations check whether an agent uses the skill correctly in a conversation.

`cases.json` has 46 scripted conversations with expected outcomes. Each run gets a temp folder, its own data file, and a simulated date of Nov 10, 2026, so real records aren't touched and the expected numbers don't change.

## Grading

Saved data and reply patterns are checked by code. For example, a purchase has to exist exactly once with the right amount and date. A second model grades criteria that code can't judge, such as whether a reply avoids recommending a course. These checks are listed as `judge` in the results.

A run passes only if every check passes. Timeouts and login failures are reported as errors and left out of the pass rate.

The results report:

- Runs passed, and cases that passed every repeat
- How often the skill was used when it should and shouldn't have been
- Runs without the skill, on the 6 safety, how-to, and advice cases only

An agent without the skill can't see the student's records, so comparing it on budget numbers would only make the skill look better than it is. On these 6 cases, both versions have what they need to answer.

## Running it

This calls a real model, which uses your Claude plan or API credit. Check what would run first:

```bash
python3 evals/run_evals.py --dry-run --split dev --reps 3 --mode both
```

Then run it:

```bash
python3 evals/run_evals.py --split dev --reps 3 --mode both
```

The script looks for `claude` on your `PATH`. Use `--agent-bin` or `CLAUDE_BIN` to point somewhere else. Results are saved to `evals/results/`.

## Keeping the result honest

The 28 `dev` cases are for finding and fixing problems. The 18 `test` cases should only be run after that work is finished, and a result should be reported once, with its commit and model version. Changing the skill after looking at `test` failures means that split can't be used as a fair measure anymore.

Claude drafted these cases and their expected outcomes, so a result shows how the skill does against those expectations and doesn't amount to independent validation. Cases written by other people would make it stronger.

The simulated date is given through an added system prompt, which real users wouldn't have. Near-miss cases can also only test the skill's description against the other skills in the temp folder, which is none, so a real setup with many skills may trigger differently.
