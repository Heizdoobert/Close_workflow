# Contributing

Thank you for helping improve this project.

## Setup

1. Use Python 3.10 or newer. Install the test dependency:

   ```bash
   python3 -m pip install -e ".[test]"
   ```

2. Run the test suite from the repository root:

   ```bash
   python3 -m pytest -q
   ```

   The whole suite must pass before you open a pull request.

## Making a change

- Every change needs a test. A bug fix needs a test that fails without the fix.
  A new behaviour needs a test that fails before the behaviour exists.
- Acceptance commands must check behaviour. A command that always succeeds
  (`true`, `echo`, `exit 0`) or only checks that a file exists does not prove
  anything. Assert on the output or the effect instead.
- Do not weaken or delete an existing test to make a change pass. Explain in the
  pull request why a test must change.
- Keep the runtime stdlib only. Do not add a runtime dependency without
  discussing it in an issue first.
- Do not commit secrets, tokens or live data. Use placeholders in examples.

## Pull requests

- Keep each pull request to one purpose and describe what changed and why.
- List the test command you ran and its result.
- Update `CHANGELOG.md` under `Unreleased` for any user-visible change.

## Reporting security issues

Do not file a public issue for a vulnerability. Follow [SECURITY.md](SECURITY.md).
