# Project Instructions

This file is read by AI coding agents (Codex, Cursor, Copilot, Claude Code, etc.) before working on this project.

## Project Overview

CS-Job-Poster is a Discord bot that aggregates U.S. CS internship and new-grad job postings from 12+ sources, deduplicates them, and publishes to Discord. See `docs/` for detailed documentation:

- `docs/STRUCTURE.md` -- file tree and module purposes
- `docs/ARCHITECTURE.md` -- system design and data flow
- `docs/SOURCES.md` -- all feed sources and their APIs

## Working Agreements

- Python 3.11+, async throughout, `discord.py` for Discord integration
- Postgres in production, SQLite in tests
- Tests live in `tests/`, run with `python -m pytest`
- New company source adapters follow the pattern in `COMPANY_SOURCE_GUIDE.md` and `job_bot/company_sources/companies/ibm.py`
- All job observations flow through `JobObservation` -> `FeedPipeline` -> `TrackChannelPublisher`
- U.S. jobs only -- every adapter must filter to U.S. locations

## MANDATORY: Post-Commit Documentation Updates

**CRITICAL RULE: Every time you create a git commit that touches Python source files, project structure, or configuration, you MUST immediately update the documentation in `docs/` before moving on to any other task.** This is not optional. Stale documentation has caused repeated issues for the team. Treat this as part of the commit itself -- the commit is not complete until docs are updated.

Steps (run these after EVERY commit with code changes):

1. Run `git diff HEAD~1..HEAD --stat` to see what changed
2. If Python files, directories, or imports changed -> you MUST update `docs/STRUCTURE.md`
3. If pipeline, runtime, publisher, database, or commands changed -> you MUST update `docs/ARCHITECTURE.md`
4. If feeds, company sources, or ATS adapters changed -> you MUST update `docs/SOURCES.md`
5. If ONLY docs, tests, or non-code files changed -> skip, no update needed

Keep docs concise -- bullet points and tables, no code snippets, no overlap between files. Create a separate commit for the doc updates.

## Do Not Modify

- `.claude/` -- Claude Code configuration (hooks, skills, settings). Do not edit, delete, or overwrite any files in this directory.
- `.env` -- local environment variables with secrets

## Key Entry Points

- `bot.py` -- starts the bot (`mvp_runtime.main()`)
- `job_bot/mvp_runtime.py` -- Discord client, poll loops, command registration
- `job_bot/github_feeds.py` -- feed definitions (`FEEDS` tuple) and fetchers
- `job_bot/company_sources/registry.py` -- company source registry
- `job_bot/feed_pipeline.py` -- shared ingestion pipeline
- `job_bot/feed_publisher.py` -- Discord publishing
