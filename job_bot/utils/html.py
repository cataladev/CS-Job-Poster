from __future__ import annotations

import html
import re
from typing import Iterable, List

from bs4 import BeautifulSoup


DETAILS_SUMMARY_RE = re.compile(r"<summary><strong>(.*?)</strong></summary>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    return collapse_whitespace(soup.get_text(" ", strip=True))


def collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def split_locations(raw: str) -> List[str]:
    if not raw:
        return []

    soup = BeautifulSoup(raw, "html.parser")
    details = soup.find("details")
    if details:
        text = details.get_text("\n", strip=True)
        lines = [collapse_whitespace(line) for line in text.splitlines() if collapse_whitespace(line)]
        if len(lines) > 1:
            return lines[1:]

    text = html.unescape(raw)
    text = text.replace("</br>", "<br>").replace("<br/>", "<br>")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = TAG_RE.sub(" ", text)
    parts = re.split(r"\n|\||;", text)
    return [collapse_whitespace(part) for part in parts if collapse_whitespace(part)]


def markdown_link_targets(raw_html: str) -> List[str]:
    if not raw_html:
        return []
    soup = BeautifulSoup(raw_html, "html.parser")
    urls = []
    for anchor in soup.find_all("a", href=True):
        urls.append(anchor["href"])
    return urls

