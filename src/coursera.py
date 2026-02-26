from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup


COURSE_PATTERNS = {
    "course": r"https://(?:www\.)?coursera\.org/learn/[\w-]+/?(?:\?.*)?$",
    "specialization": r"https://(?:www\.)?coursera\.org/specializations/[\w-]+/?(?:\?.*)?$",
    "professional_certificate": r"https://(?:www\.)?coursera\.org/professional-certificates/[\w-]+/?(?:\?.*)?$",
    "project": r"https://(?:www\.)?coursera\.org/projects/[\w-]+/?(?:\?.*)?$",
}


@dataclass
class CourseInfo:
    url: str
    kind: str
    title: Optional[str] = None
    description: Optional[str] = None
    workload: Optional[str] = None
    course_count: Optional[int] = None


def detect_kind(url: str) -> Optional[str]:
    url = url.strip()
    for kind, pattern in COURSE_PATTERNS.items():
        if re.match(pattern, url):
            return kind
    return None


def fetch_html(url: str, timeout: int = 12) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CourseInfoBot/1.0; +https://example.invalid)",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8,ru;q=0.7",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_title(soup: BeautifulSoup) -> Optional[str]:
    # Try OpenGraph
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return _clean(og["content"])

    # Fallback to <title>
    if soup.title and soup.title.text:
        return _clean(soup.title.text)

    return None


def parse_description(soup: BeautifulSoup) -> Optional[str]:
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return _clean(meta["content"])

    # Fallback: some pages use og:description
    og = soup.find("meta", attrs={"property": "og:description"})
    if og and og.get("content"):
        return _clean(og["content"])

    return None


def parse_workload(text: str) -> Optional[str]:
    """Best-effort extraction of workload hints from visible text."""
    # Common phrases can vary; keep it simple.
    # Examples: "Approx. 10 hours to complete", "3 weeks", "8 hours"
    hours = re.search(r"(?:approx\.?\s*)?(\d{1,3})\s*(?:total\s*)?hours", text, re.IGNORECASE)
    weeks = re.search(r"(\d{1,2})\s*weeks", text, re.IGNORECASE)
    months = re.search(r"(\d{1,2})\s*months", text, re.IGNORECASE)

    parts = []
    if hours:
        parts.append(f"{hours.group(1)} hours")
    if weeks:
        parts.append(f"{weeks.group(1)} weeks")
    if months:
        parts.append(f"{months.group(1)} months")

    return ", ".join(parts) if parts else None


def parse_course_count(text: str) -> Optional[int]:
    """Best-effort extraction for specializations/series."""
    # Example: "4 course series" or "4 courses"
    m = re.search(r"(\d{1,2})\s+course\s+series", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,2})\s+courses\b", text, re.IGNORECASE)
    if m:
        # This can match many unrelated strings; keep only small plausible values.
        val = int(m.group(1))
        if 1 <= val <= 30:
            return val
    return None


def get_course_info(url: str) -> CourseInfo:
    kind = detect_kind(url) or "unknown"

    try:
        html = fetch_html(url)
        soup = BeautifulSoup(html, "lxml")

        title = parse_title(soup)
        desc = parse_description(soup)

        text = soup.get_text(" ", strip=True)
        workload = parse_workload(text)
        count = parse_course_count(text) if kind in {"specialization", "professional_certificate"} else None

        return CourseInfo(
            url=url,
            kind=kind,
            title=title,
            description=desc,
            workload=workload,
            course_count=count,
        )

    except Exception:
        # Return minimal info if parsing fails
        return CourseInfo(url=url, kind=kind)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()
