---
name: update-docs
description: Review recent code changes and update project documentation (STRUCTURE.md, ARCHITECTURE.md, SOURCES.md)
user-invocable: true
---

# Update Documentation

Review recent code changes and update the project's living documentation files in `docs/`.

## Documentation Files

| File | What it covers |
|------|---------------|
| `docs/STRUCTURE.md` | Directory tree, file purposes, module organization |
| `docs/ARCHITECTURE.md` | System design: feed pipeline, company sources, publishing, deduplication, commands, database |
| `docs/SOURCES.md` | All feed sources: GitHub feeds, company APIs, ATS boards -- what each one scrapes and how |

## Steps

### 1. Understand what changed

Run these commands to see the latest commit:

```
git log -1 --oneline
git diff HEAD~1..HEAD --stat
git diff HEAD~1..HEAD
```

If docs don't exist yet (first run), skip the diff and explore the full codebase instead.

### 2. Decide which docs need updates

- New/moved/deleted Python files, new directories, changed imports -> `STRUCTURE.md`
- Changes to pipeline, runtime, publisher, database, commands, deduplication -> `ARCHITECTURE.md`
- Changes to feeds (`github_feeds.py`), company sources (`company_sources/`), ATS adapters (`ats_feeds.py`, `sources/`) -> `SOURCES.md`
- If only docs, configs, or tests changed with no structural impact -> skip, no update needed

### 3. Read current doc state

Read each doc file that needs updating. If a file doesn't exist yet, you'll create it from scratch by exploring the relevant parts of the codebase.

### 4. Update the docs

Edit only the sections affected by the change. Keep the following principles:

- **Concise and scannable** -- bullet points and tables over paragraphs
- **No code snippets** -- describe what things do, don't paste implementation
- **File paths are relative** to the project root
- **Keep it current** -- remove references to things that no longer exist
- **Don't repeat** -- each doc covers its own domain, no overlap

### 5. Report what you did

After updating, briefly state which files you updated and why (one line each).

## Doc Templates

Use these structures when creating docs from scratch:

### STRUCTURE.md

```
# Project Structure

## Root
- `bot.py` -- entry point
- ...

## job_bot/
Core application code.
- `mvp_runtime.py` -- ...
- ...

### job_bot/company_sources/
- ...

### job_bot/utils/
- ...

## tests/
- ...

## scripts/
- ...

## deploy/
- ...

## outsource/
- ...
```

### ARCHITECTURE.md

```
# Architecture

## Overview
One-paragraph summary of what the bot does.

## Feed Collection
How feeds are collected and polled.

## Company Sources
How first-party company API adapters work.

## Processing Pipeline
Deduplication, canonical merging, delivery outbox.

## Publishing
Discord embeds, channel routing, category mirrors, digests.

## Commands
Slash commands and what they do.

## Database
Schema overview, key tables, Postgres vs SQLite.

## Configuration
Key environment variables and runtime config.
```

### SOURCES.md

```
# Feed Sources

## GitHub Repository Feeds
| Source | Repository | What it tracks |
|--------|-----------|---------------|
| ... | ... | ... |

## Company API Sources
| Company | API Endpoint | Tracks |
|---------|-------------|--------|
| ... | ... | ... |

## Direct ATS Feeds
| Company | ATS Platform | Board URL |
|---------|-------------|-----------|
| ... | ... | ... |
```
