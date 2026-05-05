# Contributing to watercooler

We’re excited that you’re interested in contributing! watercooler is the open reference implementation of the Watercooler protocol. Contributions that improve the protocol, developer experience, or documentation are welcome.

## Getting Started

1. **Set up Python** – we support Python 3.10, 3.11, and 3.12.
2. **Clone and install**:
   ```bash
   git clone https://github.com/mostlyharmless-ai/watercooler.git
   cd watercooler
   python -m pip install -e ".[dev]"
   ```
3. **Run the test suite**:
   ```bash
   pytest -m "not http"
   ```
   (Add `-m http` to include integration tests that require the HTTP facade.)
4. **Optional tooling** – `pip install -e ".[dev]"` installs `mypy` for type checks. Run `mypy src/` before opening a PR when you touch type-heavy areas.

## Development Workflow

- Create a feature branch off `main`.
- Keep changes focused. Separate bug fixes, features, and documentation updates when possible.
- Run `pytest` (and `mypy` when relevant) before pushing.
- Open a pull request against `main` and fill out the PR template.
- Ensure all GitHub Actions checks pass. CI runs tests on `ubuntu-latest` across Python 3.10, 3.11, and 3.12 (matrix in `.github/workflows/ci.yml`).

## Commit Sign-off (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/) instead of a CLA. Every commit must include a `Signed-off-by` line:

```
Signed-off-by: Your Name <you@example.com>
```

Add it automatically with `git commit -s` or `git commit --signoff`. By signing off you certify compliance with the DCO.

We may introduce a Contributor License Agreement (CLA) in the future. If we do, we will provide advance notice and the CLA will not apply retroactively to contributions made under the DCO.

## Code Style

- **Python** – follow [PEP 8](https://peps.python.org/pep-0008/) with type annotations in public interfaces. The repository targets standard library tooling; keep dependencies minimal.
- **Markdown/docs** – wrap at ~100 columns, use sentence case headings, prefer relative links.
- **Tests** – new features require tests. If you fix a bug, add a regression test.

## Pre-commit hook (gitleaks secret scan + watercooler append-only protocol)

Phase 4.2 of the H1 closure plan wires a pre-commit framework that runs **two** checks on every `git commit`:

1. `gitleaks protect --verbose --redact --staged` — blocks commits that introduce known secret patterns (GitHub PATs, OpenAI/OpenRouter keys, Slack tokens, AWS/GCP/Azure credentials, etc.). The exact command comes from gitleaks's upstream pre-commit-hooks.yaml at v8.18.4; `--redact` masks the matched secret in the failure output so secrets don't leak into terminal scrollback or CI logs. Configured by [`.gitleaks.toml`](.gitleaks.toml), which extends gitleaks's default ruleset and is also used by the `copybara-dry-run.yml` CI workflow.
2. `.githooks/pre-commit` — enforces append-only edits on `watercooler/*.md` and `.watercooler/*.md` thread files (preserves orphan-branch thread integrity).

### Setup (one-time, per contributor)

> **CRITICAL — silent-overwrite hazard.** Some older docs in this repo (`.github/WATERCOOLER_SETUP.md`, `dev_docs/displaced/INSTALLATION.md`) instruct setting `git config core.hooksPath .githooks`. If that's set when you run `pre-commit install`, pre-commit writes its dispatcher to `.githooks/pre-commit`, **silently overwriting the watercooler thread-integrity enforcement script**. Always run the unset step first.

```bash
# 1. If you previously enabled core.hooksPath, unset it:
git config --unset core.hooksPath        # safe no-op if not set

# 2. Install pre-commit:
pip install pre-commit                   # macOS: brew install pre-commit

# 3. Install the hook into .git/hooks/pre-commit:
pre-commit install
```

After step 3, the `.git/hooks/pre-commit` dispatcher invokes both gitleaks and the watercooler-protocol script (the latter via the `local` hook entry in `.pre-commit-config.yaml`). Both checks run on every commit.

If you've already accidentally overwritten `.githooks/pre-commit`, restore it: `git checkout HEAD -- .githooks/pre-commit`.

### Manual scan (without committing)

```bash
pre-commit run --all-files
```

### Scan-scope note

The pre-commit hook runs `gitleaks protect --verbose --redact --staged` (staged diff only). The `copybara-dry-run.yml` CI workflow runs `gitleaks detect --no-git --source .` (full working tree). **The two are not equivalent** — secrets already present in tracked files but absent from the current staged diff are caught by CI, not by this hook. CI remains the authoritative pre-merge check; the hook is a developer-loop convenience that catches the most common case (fresh leaks in the immediate diff).

### False positives

Add an allowlist entry to `.gitleaks.toml` (paths array) rather than bypassing with `--no-verify`. Routine bypass defeats the H1 hardening.

## Filing Issues

- Use the **Bug report** template for defects and include reproduction steps.
- Use the **Feature request** template to propose enhancements or new adapters.
- If you’re unsure where to start, look for issues labeled `good first issue` or `help wanted`.

## Communication

- GitHub Discussions: ask questions, propose designs, or request help.
- For security reports, follow the process in `SECURITY.md`.

## Code of Conduct

All contributors and maintainers are expected to follow our [Code of Conduct](./CODE_OF_CONDUCT.md). Please report unacceptable behavior to the contact listed there.

Thanks for helping make Watercooler better! 🎉

