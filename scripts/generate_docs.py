"""Auto-generate docs/STRUCTURE.md from the live file tree.

Run after every commit via .githooks/post-commit to keep the structure doc
in sync with the actual codebase. No AI needed -- it walks the directory and
merges with existing descriptions.

Also checks if ARCHITECTURE.md or SOURCES.md may be stale based on which
files changed in the latest commit.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = PROJECT_ROOT / "docs"

# Directories to skip entirely
SKIP_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", "node_modules",
    ".mypy_cache", ".ruff_cache",
}

# Files/patterns to skip
SKIP_FILES = {
    ".DS_Store", "Thumbs.db", "package-lock.json",
}

# Top-level files to document (in order)
ROOT_FILES = [
    ("bot.py", "entry point, calls mvp_runtime.main()"),
    ("pyproject.toml", "project metadata, pytest config"),
    ("requirements.txt", "Python dependencies"),
    ("Makefile", "dev shortcuts"),
    ("docker-compose.yml", "local Postgres for development"),
    ("alembic.ini", "database migration config"),
    (".env.example", "environment variable template"),
    ("README.md", "project overview, setup, configuration"),
    ("CONTRIBUTING.md", "development guidelines"),
    ("AGENTS.md", "AI agent instructions (Codex, Cursor, Claude Code, etc.)"),
    ("TODO.md", "deferred work and future features"),
    ("COMPANY_SOURCE_GUIDE.md", "guide for adding company API adapters"),
]

# Known file descriptions -- add entries here when you add new files
KNOWN_DESCRIPTIONS: dict[str, str] = {
    # job_bot/ core
    "job_bot/mvp_runtime.py": "Discord client, poll loops, command registration, startup",
    "job_bot/config.py": "loads environment variables into AppConfig",
    "job_bot/models.py": "shared data models (AppConfig, JobPosting)",
    "job_bot/github_feeds.py": "feed definitions (FEEDS), JobObservation, GitHubFeedFetcher, parsers",
    "job_bot/ats_feeds.py": "builds direct ATS board feeds from company seed CSV",
    "job_bot/feed_pipeline.py": "FeedPipeline orchestrates concurrent feed collection and ingestion",
    "job_bot/feed_health.py": "SourceHealthReporter announces feed health transitions through a notifier",
    "job_bot/feed_publisher.py": "TrackChannelPublisher: Discord embeds, delivery outbox, digests",
    "job_bot/jobright_links.py": "resolves Jobright wrapper URLs to employer direct links",
    "job_bot/log_threads.py": "permanent Core feeds and Direct scrapers log threads, plus Jobright notifications",
    "job_bot/database.py": "SQLite/Postgres database layer: jobs, deliveries, feed state",
    "job_bot/dedupe.py": "multi-layer deduplication (URL normalization, title/company matching)",
    "job_bot/branding.py": "company logo/color lookup for embed styling",
    "job_bot/health_commands.py": "/health admin command (overview, feeds, deliveries views)",
    "job_bot/job_channel_commands.py": "admin slash commands for channel/role/subscription config",
    "job_bot/scrape_diagnostics.py": "ScrapeFailure error classification and context manager",
    "job_bot/scrape_status.py": "ScrapeStatusReporter posts poll results to the appropriate permanent log thread",
    "job_bot/filters.py": "feed filtering",
    # company sources
    "job_bot/company_sources/base.py": "CompanySource protocol and CompanySourceResult dataclass",
    "job_bot/company_sources/registry.py": "COMPANY_SOURCE_REGISTRY mapping",
    "job_bot/company_sources/runner.py": "CompanySourceRunner and CompanySourcePipeline",
    "job_bot/company_sources/companies/ibm.py": "IBM careers API adapter",
    "job_bot/company_sources/companies/microsoft.py": "Microsoft careers API adapter",
    "job_bot/company_sources/companies/nvidia.py": "NVIDIA careers API adapter",
    # utils
    "job_bot/utils/feed_normalization.py": "URL sanitization, location validation, text cleaning",
    "job_bot/utils/http.py": "AsyncHttpClient with retries and proxy support",
    "job_bot/utils/ats.py": "ATS platform detection from URLs",
    "job_bot/utils/html.py": "HTML tag stripping",
    "job_bot/utils/normalization.py": "row hashing, season/term detection",
    "job_bot/utils/notify.py": "Discord webhook notifications",
    "job_bot/utils/scrape.py": "scraping helpers",
    # legacy
    "job_bot/pipeline.py": "older architecture (legacy)",
    "job_bot/publisher.py": "older publisher (legacy)",
    "job_bot/scheduler.py": "older scheduler (legacy)",
    "job_bot/source_manager.py": "older source manager (legacy)",
    "job_bot/process_tracking.py": "deferred feature (legacy)",
    "job_bot/watchlist.py": "track classification",
    # outsource
    "outsource/categories.py": "9 role categories (SWE, DATA/AI, PM, etc.)",
    "outsource/mirror.py": "permanent category cards in Discord threads, routes jobs to threads",
}

# Files that indicate ARCHITECTURE.md may need updating
ARCHITECTURE_TRIGGERS = {
    "job_bot/mvp_runtime.py", "job_bot/feed_pipeline.py",
    "job_bot/feed_publisher.py", "job_bot/database.py",
    "job_bot/dedupe.py", "job_bot/health_commands.py",
    "job_bot/job_channel_commands.py", "job_bot/config.py",
    "job_bot/models.py", "outsource/mirror.py",
}

# Files that indicate SOURCES.md may need updating
SOURCES_TRIGGERS = {
    "job_bot/github_feeds.py", "job_bot/ats_feeds.py",
    "job_bot/jobright_links.py", "job_bot/feed_health.py",
    "job_bot/company_sources/registry.py",
    "job_bot/company_sources/runner.py",
}


def _list_dir(base: Path, rel: str = "") -> list[str]:
    """List files under a directory, returning relative paths."""
    files = []
    try:
        entries = sorted(base.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        return files
    for entry in entries:
        if entry.name in SKIP_DIRS or entry.name in SKIP_FILES:
            continue
        if entry.name.startswith(".") and entry.name not in {".env.example", ".gitignore"}:
            continue
        rel_path = f"{rel}/{entry.name}" if rel else entry.name
        if entry.is_dir():
            files.extend(_list_dir(entry, rel_path))
        else:
            files.append(rel_path)
    return files


def _group_by_directory(paths: list[str]) -> dict[str, list[str]]:
    """Group file paths by their parent directory."""
    groups: dict[str, list[str]] = {}
    for path in paths:
        parent = str(Path(path).parent).replace("\\", "/")
        if parent == ".":
            parent = ""
        groups.setdefault(parent, []).append(path)
    return groups


def generate_structure() -> str:
    """Generate STRUCTURE.md content from the live file tree."""
    lines = ["# Project Structure", "", "_Auto-generated by `scripts/generate_docs.py`. Do not edit manually._", ""]

    # Root files
    lines.append("## Root")
    lines.append("")
    root_documented = set()
    for filename, description in ROOT_FILES:
        if (PROJECT_ROOT / filename).exists():
            lines.append(f"- `{filename}` -- {description}")
            root_documented.add(filename)
    # Catch any new root files
    for entry in sorted(PROJECT_ROOT.iterdir(), key=lambda e: e.name.lower()):
        if entry.is_file() and entry.name not in root_documented and not entry.name.startswith("."):
            if entry.suffix in {".py", ".md", ".toml", ".yml", ".yaml", ".txt", ".cfg", ".ini"}:
                desc = KNOWN_DESCRIPTIONS.get(entry.name, "")
                suffix = f" -- {desc}" if desc else ""
                lines.append(f"- `{entry.name}`{suffix}")
    lines.append("")

    # Documented directories
    doc_dirs = [
        ("job_bot", "Core application code."),
        ("outsource", "Optional category mirror system for routing jobs into themed threads."),
        ("tests", "Test suite. Fixtures in `tests/fixtures/`."),
        ("scripts", "Utility and smoke-test scripts."),
        ("deploy", "Production deployment on a single Ubuntu VPS."),
        ("alembic", "Postgres schema migrations."),
        ("data", "Seed data (company names, career page URLs)."),
        ("config", "Runtime configuration files."),
        ("docs", "Living project documentation (auto-updated)."),
    ]

    for dir_name, dir_desc in doc_dirs:
        dir_path = PROJECT_ROOT / dir_name
        if not dir_path.is_dir():
            continue

        lines.append(f"## {dir_name}/")
        lines.append("")
        lines.append(dir_desc)
        lines.append("")

        all_files = _list_dir(dir_path, dir_name)
        groups = _group_by_directory(all_files)

        # Files directly in this directory
        direct_files = groups.get(dir_name, [])
        for filepath in direct_files:
            filename = Path(filepath).name
            if filename == "__init__.py":
                continue
            desc = KNOWN_DESCRIPTIONS.get(filepath, "")
            suffix = f" -- {desc}" if desc else ""
            lines.append(f"- `{filename}`{suffix}")

        # Subdirectories -- collect immediate child dirs, then show all files under them
        immediate_subdirs = sorted({
            dir_name + "/" + key.replace(dir_name + "/", "").split("/")[0]
            for key in groups
            if key != dir_name and key.startswith(dir_name + "/")
        })
        for subdir in immediate_subdirs:
            lines.append("")
            lines.append(f"### {subdir}/")
            lines.append("")

            # All files under this subdir (including nested)
            sub_files = []
            for key in sorted(groups):
                if key == subdir or key.startswith(subdir + "/"):
                    sub_files.extend(groups[key])

            for filepath in sub_files:
                filename = Path(filepath).name
                if filename == "__init__.py":
                    continue
                desc = KNOWN_DESCRIPTIONS.get(filepath, "")
                suffix = f" -- {desc}" if desc else ""
                lines.append(f"- `{filename}`{suffix}")

        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def check_staleness() -> list[str]:
    """Check if ARCHITECTURE.md or SOURCES.md may be stale based on the latest commit."""
    warnings = []
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD~1..HEAD", "--name-only"],
            capture_output=True, text=True, cwd=PROJECT_ROOT,
        )
        if result.returncode != 0:
            return warnings
        changed = set(result.stdout.strip().splitlines())
    except (OSError, subprocess.SubprocessError):
        return warnings

    # Check for new company sources
    new_sources = [
        f for f in changed
        if f.startswith("job_bot/company_sources/companies/") and f.endswith(".py")
    ]
    if new_sources:
        SOURCES_TRIGGERS.update(new_sources)

    arch_stale = changed & ARCHITECTURE_TRIGGERS
    sources_stale = changed & SOURCES_TRIGGERS

    if arch_stale:
        warnings.append(
            f"docs/ARCHITECTURE.md may be stale -- these files changed: {', '.join(sorted(arch_stale))}"
        )
    if sources_stale:
        warnings.append(
            f"docs/SOURCES.md may be stale -- these files changed: {', '.join(sorted(sources_stale))}"
        )
    return warnings


def main() -> int:
    DOCS_DIR.mkdir(exist_ok=True)

    # Always regenerate STRUCTURE.md
    structure_path = DOCS_DIR / "STRUCTURE.md"
    new_content = generate_structure()
    old_content = structure_path.read_text(encoding="utf-8") if structure_path.exists() else ""

    if new_content != old_content:
        structure_path.write_text(new_content, encoding="utf-8")
        print(f"Updated docs/STRUCTURE.md")
        # Stage and commit the doc update
        subprocess.run(["git", "add", "docs/STRUCTURE.md"], cwd=PROJECT_ROOT)
    else:
        print("docs/STRUCTURE.md is up to date")

    # Check staleness of other docs
    warnings = check_staleness()
    for warning in warnings:
        print(f"WARNING: {warning}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
