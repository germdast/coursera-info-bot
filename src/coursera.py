from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

COURSE_PATTERNS = {
    "course": r"^/learn/[\w-]+$",
    "specialization": r"^/specializations/[\w-]+$",
    "professional_certificate": r"^/professional-certificates/[\w-]+$",
    "project": r"^/projects/[\w-]+$",
}

SEP_OPT = r"(?:[·•\-\u2013\u2014:]\s*)?"
MODULE_HOURS_RE = re.compile(
    rf"Module\s*\d+\s*{SEP_OPT}(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b(?:\s*to\s*complete)?",
    re.IGNORECASE,
)
COURSE_HOURS_RE = re.compile(
    rf"Course\s*\d+\s*{SEP_OPT}(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b",
    re.IGNORECASE,
)
GENERIC_HOURS_TO_COMPLETE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\s*to\s*complete",
    re.IGNORECASE,
)

@dataclass
class CourseInfo:
    url: str
    kind: str
    title: Optional[str] = None
    description: Optional[str] = None
    workload_hint: Optional[str] = None
    course_count: Optional[int] = None
    total_hours: Optional[float] = None
    sum_basis: Optional[str] = None
    items_count: Optional[int] = None

def canonicalize_url(url: str) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    m = re.search(r"https?://\S+", url)
    if m:
        url = m.group(0)

    try:
        p = urlparse(url)
    except Exception:
        return None

    host = (p.netloc or "").lower()
    if not host.endswith("coursera.org"):
        return None

    path = p.path or ""
    if path.startswith("/programs/"):
        parts = path.split("/")
        if len(parts) >= 5:
            path = "/" + "/".join(parts[3:])

    parts = [x for x in path.split("/") if x]
    if len(parts) < 2:
        return None

    base_path = f"/{parts[0]}/{parts[1]}"

    if not any(re.match(pat, base_path) for pat in COURSE_PATTERNS.values()):
        return None

    return urlunparse(("https", "www.coursera.org", base_path, "", "", ""))

def detect_kind(url: str) -> Optional[str]:
    cu = canonicalize_url(url)
    if not cu:
        return None
    path = urlparse(cu).path
    for kind, pat in COURSE_PATTERNS.items():
        if re.match(pat, path):
            return kind
    return None

def is_supported_coursera_url(url: str) -> bool:
    return detect_kind(url) is not None

_session: Optional[requests.Session] = None

def _get_session() -> requests.Session:
    global _session
    if _session is not None:
        return _session
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    _session = s
    return s

def fetch_html(url: str, timeout: int = 18) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CourseraInfoBot/1.0)",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8,ru;q=0.7",
    }
    s = _get_session()
    resp = s.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp.text

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def parse_title(soup: BeautifulSoup) -> Optional[str]:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return _clean(og["content"])
    if soup.title and soup.title.text:
        return _clean(soup.title.text)
    return None

def parse_description(soup: BeautifulSoup) -> Optional[str]:
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return _clean(meta["content"])
    og = soup.find("meta", attrs={"property": "og:description"})
    if og and og.get("content"):
        return _clean(og["content"])
    return None

def parse_course_count(text: str) -> Optional[int]:
    m = re.search(r"(\d{1,2})\s+course\s+series", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,2})\s+courses\b", text, re.IGNORECASE)
    if m:
        val = int(m.group(1))
        if 1 <= val <= 30:
            return val
    return None

def parse_workload_hint(text: str) -> Optional[str]:
    hours = re.search(r"(?:approx\.?\s*)?(\d{1,3})\s*(?:total\s*)?(?:hours?|hrs?)\b", text, re.IGNORECASE)
    weeks = re.search(r"(\d{1,2})\s*weeks\b", text, re.IGNORECASE)
    months = re.search(r"(\d{1,2})\s*months\b", text, re.IGNORECASE)

    parts = []
    if hours:
        parts.append(f"{hours.group(1)} hours")
    if weeks:
        parts.append(f"{weeks.group(1)} weeks")
    if months:
        parts.append(f"{months.group(1)} months")

    return ", ".join(parts) if parts else None

def _sum_matches(pattern: re.Pattern, text: str) -> Tuple[Optional[float], int]:
    total = 0.0
    count = 0
    for m in pattern.finditer(text):
        try:
            total += float(m.group(1))
            count += 1
        except Exception:
            pass
    if count == 0:
        return None, 0
    return total, count

def _try_next_data_total_hours(soup: BeautifulSoup) -> Optional[float]:
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return None
    try:
        data = json.loads(script.string)
    except Exception:
        return None
    blob = json.dumps(data)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\s*to\s*complete", blob, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None

def parse_total_hours(kind: str, soup: BeautifulSoup) -> Tuple[Optional[float], Optional[str], Optional[int]]:
    text = soup.get_text(" ", strip=True)
    if kind == "course":
        total, count = _sum_matches(MODULE_HOURS_RE, text)
        if total is not None:
            return total, "modules", count
    elif kind in {"specialization", "professional_certificate"}:
        total, count = _sum_matches(COURSE_HOURS_RE, text)
        if total is not None:
            return total, "courses", count

    m = GENERIC_HOURS_TO_COMPLETE_RE.search(text)
    if m:
        return float(m.group(1)), "hint", None

    total = _try_next_data_total_hours(soup)
    if total is not None:
        return total, "hint", None

    return None, None, None

def get_course_info(url: str) -> CourseInfo:
    cu = canonicalize_url(url) or url
    kind = detect_kind(cu) or "unknown"

    try:
        html = fetch_html(cu)
        soup = BeautifulSoup(html, "lxml")

        title = parse_title(soup)
        desc = parse_description(soup)
        text = soup.get_text(" ", strip=True)

        workload_hint = parse_workload_hint(text)
        course_count = parse_course_count(text) if kind in {"specialization", "professional_certificate"} else None
        total, basis, items = parse_total_hours(kind, soup)

        return CourseInfo(
            url=cu,
            kind=kind,
            title=title,
            description=desc,
            workload_hint=workload_hint,
            course_count=course_count,
            total_hours=total,
            sum_basis=basis,
            items_count=items,
        )
    except Exception:
        return CourseInfo(url=cu, kind=kind)
