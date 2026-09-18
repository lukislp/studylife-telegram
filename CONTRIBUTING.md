# Contributing to studylife-telegram

Thanks for taking the time. This is a single-maintainer project, so the process is deliberately
small - but it is the same for every change, including the maintainer's own.

## How changes get in

1. Open an issue first for anything bigger than a typo or an obvious bug fix, so the direction can
   be agreed before you spend time on it. Use the templates under `.github/ISSUE_TEMPLATE/`.
2. Fork the repository (or branch, if you have write access) and make your change on a branch.
3. Open a pull request against `main`. The pull-request template asks for what changed and why.
4. `main` is protected: a PR merges only once its required checks are green
   ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)'s `lint` and `test` jobs, plus
   `dependency-review.yml`'s `review / dependency-review`) and the branch is up to date with
   `main`. Nobody pushes to `main` directly, not even the maintainer - the release chain itself
   (semantic-release, the Docker build, the `k8s/` image-tag bump) runs as a push-triggered job on
   `main` afterwards, not as part of the PR.

## What a pull request needs

- **Conventional Commits.** The version and `CHANGELOG.md` are generated from the commit messages
  by semantic-release (`fix:` = patch release, `feat:` = minor release; `build:`/`ci:`/`docs:`/
  `test:`/`chore:` produce no release). If the PR is squash-merged, the squashed commit message -
  usually the PR title - is what gets analyzed, so give the PR a Conventional Commit title too.
- **Tests for new functionality.** New behaviour and bug fixes come with tests under `tests/`
  (`uv run pytest`). Per the README, the modules worth testing directly are the ones without I/O -
  `verify.py` (both authentication paths) and `commands.py` (parsing, formatting, course matching)
  - the FastAPI layer in `main.py` stays thin enough to read rather than needing its own tests.
- **Linting and formatting.** `uv run ruff check .` and `uv run ruff format --check .` both run as
  the required `lint` check; run `uv run ruff format .` before pushing. There is no separate mypy
  check in CI yet, but the project is configured for `strict = true` (see `pyproject.toml`) -
  run `uv run mypy` locally before pushing rather than relying on CI to catch a type error.
- **Security model.** Telegram does not sign webhook requests - the `secret_token` from
  `setWebhook` is the only proof a request actually came from Telegram, backed by a chat-ID
  allowlist the service refuses to start without (see `verify.py` and the README's Security
  section). Anything that touches request verification or the allowlist needs a test proving the
  rejection path still rejects, not just the happy path.
- **Ambiguous input stays ambiguous.** A course name or command argument that could match more
  than one course is rejected rather than guessed - the wrong course silently attributed would
  skew the grade/ECTS correlations StudyLife computes from session history. Keep that behaviour
  when touching `commands.py`.

## Running things locally

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .   # or `uv run ruff format .` to fix
uv run mypy
```

See the README's [Development](README.md#development) section and the Docker/`k8s/` layout for
how a change reaches the running service.

## Security issues

Please do not open a public issue for a vulnerability - use the private reporting path described
in [SECURITY.md](SECURITY.md).
