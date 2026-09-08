"""Broad presentation categories; these never exclude jobs from ingestion."""

from __future__ import annotations

import re
from dataclasses import dataclass

from job_bot.utils.html import strip_html


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    roles: str


CATEGORIES = {
    category.key: category
    for category in (
        Category("swe", "SWE", "Software engineering, frontend, backend, full stack, mobile, "
                 "embedded software, software systems engineering, platform engineering, "
                 "SRE, DevOps, and test automation."),
        Category("data_ai", "DATA / AI", "Data engineering, data science, machine learning, "
                 "AI engineering, and applied research."),
        Category("pm", "PM", "Product management, technical program management, and "
                 "technical project management."),
        Category("it_security", "IT / SECURITY", "IT support, systems administration, network "
                 "administration, cybersecurity, security operations, and cloud administration."),
        Category("quant", "QUANT", "Quantitative research, quantitative trading, and "
                 "explicitly quantitative developer roles."),
        Category("analyst", "ANALYST", "Business analysts, data analysts, business intelligence, "
                 "product analysts, and financial analysts."),
        Category("hardware", "HARDWARE", "Electrical engineering, computer hardware, chip "
                 "design, FPGA, and physical robotics engineering."),
        Category("design", "DESIGN", "UX/UI, product design, UX research, and interaction design."),
        Category("other", "OTHER", "Roles that don’t clearly fit an existing category or "
                 "don’t provide enough information to classify confidently."),
    )
}

# Display order is independent of which category receives a listing first.
CATEGORY_ORDER = (
    "swe", "data_ai", "pm", "it_security", "quant", "analyst", "hardware", "design", "other",
)

# Specific role families precede the broad software bucket. Company names are
# deliberately excluded: a software engineer at a quant firm still belongs in SWE.
ROLE_PATTERNS = {
    "pm": r"(?:product|program|project) (?:\w+ ){0,2}(?:manager|management)|\b[at]?pm\b",
    "quant": r"quant(?:itative)?(?: |$)|algorithmic trad(?:er|ing)",
    "design": r"\b(?:ux|ui)\b|user (?:experience|interface)|product design|interaction design",
    "data_ai": r"data (?:engineer|scien)|machine learning|\b(?:ml|ai|nlp)\b|"
               r"artificial intelligence|deep learning|computer vision|applied (?:scientist|research)",
    "it_security": r"cyber ?security|information security|security (?:analyst|operations)|"
                   r"soc analyst|penetration test|\b(?:it|help ?desk|sysadmin)\b|"
                   r"(?:systems?|network|cloud|database) administrat|technical support|"
                   r"desktop support|network engineer",
    "hardware": r"hardware|electrical|electronics|\b(?:fpga|asic|rtl|vlsi)\b|"
                r"chip design|semiconductor|silicon|robotics|mechatronics|physical systems",
    "swe": r"software|\bswe\b|front ?end|back ?end|full ?stack|developer|programmer|"
           r"mobile|embedded|firmware|platform engineer|site reliability|\bsre\b|"
           r"dev ?ops|devsecops|test automation|automation (?:test |qa )?engineer|"
           r"\bsdet\b|quality assurance engineer|cloud engineer|gameplay",
    "analyst": r"analyst|analytics|business intelligence|\bbi\b",
}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", strip_html(text).casefold()).strip()


def _matches(text: str, key: str) -> bool:
    return bool(re.search(r"\b(?:" + ROLE_PATTERNS[key] + r")", text))


def classify_role(title: str, summary: str = "") -> Category:
    title_text = _normalize(title)
    # The pronoun "it" is not evidence of an IT role in a description.
    summary_text = _normalize(re.sub(r"\b[Ii]t\b", "", summary))
    # Security software and robotics software are software roles. More specific
    # quant, product, data, and design titles still take precedence.
    for key in ("pm", "quant", "design", "data_ai"):
        if _matches(title_text, key):
            return CATEGORIES[key]
    if _matches(title_text, "analyst"):
        return CATEGORIES["it_security" if _matches(title_text, "it_security") else "analyst"]
    if re.search(r"\b(?:software|firmware|embedded software)\b", title_text):
        return CATEGORIES["swe"]
    for key in ("it_security", "analyst", "hardware", "swe"):
        if _matches(title_text, key):
            return CATEGORIES[key]
    if "security engineer" in title_text:
        return CATEGORIES["it_security"]
    if re.search(r"\bsystems? engineer", title_text):
        for key in ("it_security", "hardware"):
            if _matches(summary_text, key):
                return CATEGORIES[key]
        return CATEGORIES["swe"]
    # Only use descriptions when they point to one family. Mixed boilerplate and
    # vague titles must not cause arbitrary placement based on the first keyword.
    matches = {key for key in ROLE_PATTERNS if _matches(summary_text, key)}
    return CATEGORIES[matches.pop()] if len(matches) == 1 else CATEGORIES["other"]
