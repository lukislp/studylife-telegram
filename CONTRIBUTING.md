# Contributing to StudyLife

Thanks for taking the time. StudyLife is a single-maintainer project, so the process is deliberately
small - but it is the same for every change, including the maintainer's own.

## How changes get in

1. Open an issue first for anything bigger than a typo or an obvious bug fix, so the direction can
   be agreed before you spend time on it. Use the templates under `.github/ISSUE_TEMPLATE/`.
2. Fork the repository (or branch, if you have write access) and make your change on a branch.
3. Open a pull request against `main`. The pull-request template asks for what changed and why.
4. `main` is protected: a PR merges only after the whole test stage of
   [`.github/workflows/ci-cd.yml`](.github/workflows/ci-cd.yml) is green and the branch is up to
   date with `main` (enable auto-merge and it lands on its own once that is the case). Nobody pushes
   to `main` directly, not even the maintainer.

## What a pull request needs

- **Conventional Commits.** The version and the changelog are generated from the commit messages
  (`feat:` = minor release, `fix:` = patch release, `build:`/`ci:`/`docs:`/`test:` = no release).
  Squash-merge keeps the PR title as the commit message, so give the PR a Conventional Commit
  title.
- **Tests for new functionality.** New features and bug fixes come with tests in the matching
  project under `tests/` (`StudyLife.Shared.Tests`, `StudyLife.Server.Tests`,
  `StudyLife.Tts.Tests`). A PR that adds behaviour without a test is asked to add one. The
  coverage badge in the README is regenerated from the merged coverage report on every release
  and is expected not to drop.
- **Formatting and warnings.** `dotnet format StudyLife.sln --verify-no-changes` runs as a required
  check (`test-lint`); run `dotnet format StudyLife.sln` before pushing. The build runs with the
  .NET analyzers on; do not silence warnings without saying why in the PR.
- **All 26 languages.** UI strings live in the resource tables checked by `tools/check-i18n.py`
  (required check `test-i18n`); a new string needs an entry in every language, machine-translated
  is fine, mark it as such.
- **API contract.** A change to controllers or DTOs regenerates `docs/api/openapi.json`; commit
  the regenerated file (`test-openapi-contract` fails otherwise) - add-ons and the CLI build
  against it.
- **Database.** Schema changes come with an EF Core migration (`test-ef-migrations` verifies the
  model and the migrations agree).
- **No behaviour behind demo mode.** New endpoints and features must stay read-only in demo mode
  (see `docs/ARCHITECTURE.md`).

## Running things locally

```bash
dotnet restore StudyLife.sln
dotnet build StudyLife.sln
dotnet test StudyLife.sln
dotnet format StudyLife.sln --verify-no-changes
python3 tools/check-i18n.py
```

See the README's "Development" section for the dev server and the k3d cluster that mirrors
production.

## Security issues

Please do not open a public issue for a vulnerability - use the private reporting path described
in [SECURITY.md](SECURITY.md).
