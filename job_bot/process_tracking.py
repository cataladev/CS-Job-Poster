from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from rapidfuzz import fuzz


# Role-scoped process tracking is intentionally strict about privacy and content
# safety. These helpers normalize all user-facing process state in one place so
# the bot, database layer, and tests agree on track/season/status semantics.
PROCESS_VISIBILITY_DEFAULT = "private"
PROCESS_VISIBILITY_LABELS = {
    "matched": "Matched Only",
    "open": "Open",
    "private": "Private",
}
PROCESS_STATUS_LABELS = {
    "save": "Saved",
    "hide": "Hidden",
    "applied": "Applied",
    "oa_received": "OA Received",
    "interview": "Interview",
    "rejected": "Rejected",
    "offer": "Offer",
}
PROCESS_STATUS_ALIASES = {
    "save": "save",
    "saved": "save",
    "bookmark": "save",
    "bookmarked": "save",
    "hide": "hide",
    "hidden": "hide",
    "applied": "applied",
    "apply": "applied",
    "application_sent": "applied",
    "oa": "oa_received",
    "oa_received": "oa_received",
    "oareceived": "oa_received",
    "online_assessment": "oa_received",
    "onlineassessment": "oa_received",
    "interview": "interview",
    "interviews": "interview",
    "rejected": "rejected",
    "reject": "rejected",
    "offer": "offer",
    "offered": "offer",
}
PROCESS_ACTIVE_STATUSES = {"save", "applied", "oa_received", "interview"}
PROCESS_STALE_THRESHOLDS = {
    "save": timedelta(days=5),
    "applied": timedelta(days=14),
    "oa_received": timedelta(days=7),
    "interview": timedelta(days=10),
}
PROCESS_TRACK_ALIASES = {
    "intern": "internship",
    "internship": "internship",
    "newgrad": "new_grad",
    "new_grad": "new_grad",
    "new grad": "new_grad",
}
PROCESS_NOTE_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PROCESS_NOTE_PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
PROCESS_NOTE_LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)
PROCESS_NOTE_BLOCKLIST = (
    "screenshot",
    "screen shot",
    "screen recording",
    "recording",
    "recruiter email",
    "interview question",
    "oa question",
    "online assessment question",
    "take-home prompt",
    "google drive",
    "docs.google.com",
    "dropbox.com",
    "discordapp.com",
    "cdn.discordapp.com",
)
PROCESS_NOTE_RATE_LIMIT_WINDOW_SECONDS = 15 * 60
PROCESS_NOTE_RATE_LIMIT_MAX = 5
PROCESS_STATUS_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
PROCESS_STATUS_RATE_LIMIT_MAX = 10
PROCESS_REPORT_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
PROCESS_REPORT_RATE_LIMIT_MAX = 10


def normalize_process_visibility(value: str) -> Optional[str]:
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "matched": "matched",
        "match": "matched",
        "matched_only": "matched",
        "open": "open",
        "everyone": "open",
        "public": "open",
        "private": "private",
        "hidden": "private",
        "off": "private",
    }
    return aliases.get(cleaned)


def format_process_visibility(visibility: str) -> str:
    return PROCESS_VISIBILITY_LABELS.get(visibility, visibility.replace("_", " ").title())


def normalize_process_status(value: str) -> Optional[str]:
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    return PROCESS_STATUS_ALIASES.get(cleaned)


def format_process_status(status: str) -> str:
    return PROCESS_STATUS_LABELS.get(status, status.replace("_", " ").title())


def normalize_process_track(value: str) -> Optional[str]:
    cleaned = value.strip().lower().replace("-", "_")
    return PROCESS_TRACK_ALIASES.get(cleaned)


def normalize_season_term(value: str) -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        return ""
    parts = cleaned.split()
    if len(parts) >= 2 and parts[1].isdigit():
        return f"{parts[0].title()} {parts[1]}"
    return cleaned.title()


def normalize_role_title(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return " ".join(cleaned.split())


def infer_process_track(*, role_title: str, canonical_track: Optional[str] = None) -> Optional[str]:
    if canonical_track in {"internship", "new_grad"}:
        return canonical_track
    lowered = role_title.lower()
    if "intern" in lowered or "co-op" in lowered or "coop" in lowered:
        return "internship"
    if any(token in lowered for token in ("new grad", "new graduate", "engineer i", "engineer 1", "entry level")):
        return "new_grad"
    return None


def process_scope_score(role_title: str, candidate_title: str, season_term: str, candidate_season_term: str) -> float:
    title_score = fuzz.token_set_ratio(role_title.lower(), candidate_title.lower())
    if season_term and candidate_season_term and season_term.lower() != candidate_season_term.lower():
        return title_score * 0.7
    return float(title_score)


def clean_process_note(note: str) -> str:
    return " ".join(note.strip().split())[:400]


def validate_process_note(note: str) -> Optional[str]:
    if not note:
        return "Notes cannot be empty."
    if PROCESS_NOTE_EMAIL_RE.search(note):
        return "Notes cannot include email addresses."
    if PROCESS_NOTE_PHONE_RE.search(note):
        return "Notes cannot include phone numbers."
    if PROCESS_NOTE_LINK_RE.search(note):
        return "Notes cannot include links or attachments."
    lowered = note.lower()
    # Keep process notes useful for progress updates, but block content that is
    # likely to leak confidential recruiting material or personally identifying
    # information into the shared community view.
    for blocked in PROCESS_NOTE_BLOCKLIST:
        if blocked in lowered:
            return "Notes cannot include leaked or private process content."
    return None


def build_process_next_action(status: str, updated_at: str) -> str:
    if status not in PROCESS_ACTIVE_STATUSES:
        return "Closed."
    try:
        updated = datetime.fromisoformat(updated_at)
    except ValueError:
        return "Update this entry."
    age = datetime.now(timezone.utc) - updated
    threshold = PROCESS_STALE_THRESHOLDS.get(status)
    if threshold and age < threshold:
        if status == "save":
            return "Decide whether to apply or hide it."
        if status == "applied":
            return "Wait for movement, then update when you hear back."
        if status == "oa_received":
            return "Complete the OA or update once it is done."
        if status == "interview":
            return "Update after your next interview step."
    if status == "save":
        return "This looks stale. Apply, hide it, or update the role."
    if status == "applied":
        return "Reach out if you have a contact, or update when you hear back."
    if status == "oa_received":
        return "If this is still live, complete it or update the outcome."
    if status == "interview":
        return "Follow up or update where the process is stuck."
    return "Update this entry."


def parse_process_scope_parts(value: str) -> list[str]:
    return [part.strip() for part in value.split("|") if part.strip()]


def parse_process_status_command(value: str) -> Optional[tuple[str, str, str, str, Optional[str]]]:
    parts = parse_process_scope_parts(value)
    if len(parts) == 4:
        company, role_title, season_term, status_raw = parts
        status = normalize_process_status(status_raw)
        if not status:
            return None
        return company, role_title, normalize_season_term(season_term), status, None
    if len(parts) == 5:
        company, role_title, track_raw, season_term, status_raw = parts
        status = normalize_process_status(status_raw)
        track = normalize_process_track(track_raw)
        if not status or not track:
            return None
        return company, role_title, normalize_season_term(season_term), status, track
    return None


def parse_process_note_command(value: str) -> Optional[tuple[str, str]]:
    if "|" not in value:
        return None
    scope_raw, note = [part.strip() for part in value.split("|", 1)]
    if not scope_raw or not note:
        return None
    return scope_raw, note
