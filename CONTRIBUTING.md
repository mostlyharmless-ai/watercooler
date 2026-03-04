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
- Ensure all GitHub Actions checks pass. CI runs tests on Ubuntu and macOS across Python 3.10–3.12.

## Commit Sign-off (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/) instead of a CLA. Every commit must include a `Signed-off-by` line:

```
Signed-off-by: Your Name <you@example.com>
```

Add it automatically with `git commit -s` or `git commit --signoff`. By signing off you certify compliance with the DCO.

## Code Style

- **Python** – follow [PEP 8](https://peps.python.org/pep-0008/) with type annotations in public interfaces. The repository targets standard library tooling; keep dependencies minimal.
- **Markdown/docs** – wrap at ~100 columns, use sentence case headings, prefer relative links.
- **Tests** – new features require tests. If you fix a bug, add a regression test.

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

