# Contributing

This project is a Discord bot that aggregates U.S. software internship and new-grad jobs, deduplicates them across multiple sources, and posts only new canonical roles.

If you want to contribute, use this file as the working agreement for local setup, issue filing, and pull requests.

## Before You Start

- Read [README.md](/Users/carlos/Development/Job%20Bot/README.md) first.
- Use Python `3.11`.
- Create a local `.env` from [.env.example](/Users/carlos/Development/Job%20Bot/.env.example).
- Do not commit secrets, tokens, or local database files.

## Local Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Useful commands:

```bash
source .venv/bin/activate
python bot.py
pytest tests -q
PYTHONPYCACHEPREFIX=.pycache python3 -m py_compile $(find job_bot tests -name '*.py' -print) bot.py
```

If your shell has temp-dir issues:

```bash
mkdir -p .tmp
TMPDIR=$PWD/.tmp pytest tests -q
```

## Contribution Workflow

1. Start from `main`.
2. Create a focused branch.
3. Make the smallest coherent change that solves one problem.
4. Run tests before you open or update a PR.
5. Update docs when behavior, commands, or configuration change.

Branch naming used in this repo:

- `feature/<name>`
- `fix/<name>`
- `docs/<name>`
- `chore/<name>`

## What Good Changes Look Like

- Keep source adapters isolated; do not mix source-specific parsing into shared code.
- Prefer strict dedupe and strict branding over aggressive guessing.
- Preserve no-repost behavior across restarts.
- Keep Discord output readable and stable.
- Add targeted comments only where a decision or invariant is easy to miss.

## Tests

At minimum, contributors should run:

- the full pytest suite
- the compile pass shown above

Add or update tests when you change:

- source parsing
- filtering behavior
- dedupe logic
- command permissions
- branding/logo resolution
- watchlist import or approval flow

## Filing Issues

Use GitHub Issues for:

- bug reports
- source regressions
- false-positive or false-negative dedupe cases
- company watchlist requests that need engineering work
- feature proposals

Please include concrete examples:

- source URL
- expected behavior
- actual behavior
- relevant logs or screenshots
- whether the issue affects internships, new-grad, or both

## Pull Request Notes

- Keep PRs scoped.
- Explain user-visible behavior changes clearly.
- Mention any new env vars, command changes, or schema changes.
- Call out follow-up work with `TODO` comments only when it is a real next step, not a vague idea.

## Areas That Usually Need Extra Care

- `job_bot/pipeline.py`
  Incremental posting and refresh behavior.
- `job_bot/dedupe.py`
  Avoid merging distinct roles accidentally.
- `job_bot/branding.py`
  Do not add guessed company-domain logic.
- `job_bot/watchlist.py`
  Keep watchlist imports idempotent and strict about branded homepages.
- `job_bot/bot.py`
  Admin-only commands should fail before any side effects occur.
