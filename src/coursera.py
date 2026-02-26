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


# ----------------------------
# Supported Coursera URL prefixes
# ----------------------------
_ALLOWED_PREFIXES = {
    "learn": "course",
    "specializations": "specialization",
    "professional-certificates": "professional_certificate",
    "projects": "project",
}

# ----------------------------
# Workload patterns (best-effort)
# ----------------------------
# separators may disappear in soup.get_text -> optional
SEP_OPT = r"(?:[·•\-\u2013\u2014:]\s*)?"

# strict-ish patterns
MODULE_STRICT_RE = re.compile(
    rf"Module\s*\d+\s*{SEP_OPT}(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\b(?:\s*to\s*complete)?",
    re.IGNORECASE,
)
COURSE_STRICT_RE = re.compile(
    rf"Course\s*\d+\s*{SEP_OPT}(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)

# fuzzy fallbacks if Coursera inserts words between label and duration
MODULE_FUZZY_RE = re.compile(
    r"Module\s*\d+.{0,80}?(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)
COURSE_FUZZY_RE = re.compile(
    r"Course\s*\d+.{0,80}?(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)

GENERIC_HOURS_TO_COMPLETE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(hours?|hrs?)\s*to\s*complete",
    re.IGNORECASE,
)


@dataclass
class CourseInfo:
    url: str
    kind: str  # course | specialization | professional_certificate | project | unknown
    title: Optional[str] = None
    description: Optional[str] = None

    workload_hint: Optional[str] = None
    course_count: Optional[int] = None

    total_hours: Optional[float] = None
    sum_basis: Optional[str] = None  # modules | courses | hint
    items_count: Optional[int] = None


# ----------------------------
# URL handling
# ----------------------------
def _strip_query_fragment(u: str) -> str:
    p = urlparse(u)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def sanitize_fetch_url(url: str) -> Optional[str]:
    """
    Keeps extra path like /home/welcome (important for some courses),
    but removes query/fragment and handles /programs/<program>/ prefix.
    """
    if not url:
        return None

    url = url.strip()
    m = re.search(r"https?://\S+", url)
    if m:
        url = m.group(0).rstrip(").,]>'\"")

    try:
        p = urlparse(url)
    except Exception:
        return None

    host = (p.netloc or "").lower()
    if not host.endswith("coursera.org"):
        return None

    path = p.path or ""

    # strip /programs/<program>/ prefix but keep the rest of path (including /home/welcome)
    if path.startswith("/programs/"):
        parts = path.split("/")
        # ['', 'programs', '{program}', '<type>', '<slug>', ...]
        if len(parts) >= 5:
            path = "/" + "/".join(parts[3:])

    # ensure starts with supported prefix
    seg = [x for x in path.split("/") if x]
    if len(seg) < 2:
        return None
    if seg[0] not in _ALLOWED_PREFIXES:
        return None

    # keep full remaining path (home/welcome/etc), drop query/fragment
    return urlunparse(("https", "www.coursera.org", path.rstrip("/"), "", "", ""))


def canonicalize_url(url: str) -> Optional[str]:
    """
    Canonical for dedupe/display: keep only first 2 segments /<type>/<slug>.
    """
    fetch_u = sanitize_fetch_url(url)
    if not fetch_u:
        return None
    p = urlparse(fetch_u)
    seg = [x for x in (p.path or "").split("/") if x]
    if len(seg) < 2:
        return None
    base_path = f"/{seg[0]}/{seg[1]}"
    return urlunparse(("https", "www.coursera.org", base_path, "", "", ""))


def detect_kind(url: str) -> Optional[str]:
    cu = canonicalize_url(url)
    if not cu:
        return None
    p = urlparse(cu)
    seg = [x for x in (p.path or "").split("/") if x]
    if not seg:
        return None
    return _ALLOWED_PREFIXES.get(seg[0])


def is_supported_coursera_url(url: str) -> bool:
    return detect_kind(url) is not None


# ----------------------------
# HTTP session with retries
# ----------------------------
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


# ----------------------------
# Parsing helpers
# ----------------------------
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
    hours = re.search(
        r"(?:approx\.?\s*)?(\d{1,3})\s*(?:total\s*)?(?:hours?|hrs?)\b",
        text,
        re.IGNORECASE,
    )
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


def _to_hours(value: float, unit: str) -> float:
    u = unit.lower()
    if u.startswith("min"):
        return value / 60.0
    return value


def _sum_duration_matches(pattern: re.Pattern, text: str) -> Tuple[Optional[float], int]:
    total = 0.0
    count = 0
    for m in pattern.finditer(text):
        try:
            val = float(m.group(1))
            unit = m.group(2)
            total += _to_hours(val, unit)
            count += 1
        except Exception:
            pass
    if count == 0:
        return None, 0
    return total, count


def _try_next_data_sum(kind: str, soup: BeautifulSoup) -> Tuple[Optional[float], int]:
    """
    Very best-effort: apply the same regex on __NEXT_DATA__ JSON string.
    Sometimes syllabus appears there even if not in visible text.
    """
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return None, 0

    try:
        data = json.loads(script.string)
    except Exception:
        return None, 0

    blob = json.dumps(data)
    if kind == "course":
        total, count = _sum_duration_matches(MODULE_FUZZY_RE, blob)
        return total, count
    if kind in {"specialization", "professional_certificate"}:
        total, count = _sum_duration_matches(COURSE_FUZZY_RE, blob)
        return total, count
    return None, 0


def parse_total_hours(kind: str, soup: BeautifulSoup) -> Tuple[Optional[float], Optional[str], Optional[int]]:
    """
    Rules:
      - course -> sum module hours
      - specialization/pro cert -> sum course hours
    """
    text = soup.get_text(" ", strip=True)

    if kind == "course":
        total, count = _sum_duration_matches(MODULE_STRICT_RE, text)
        if total is None:
            total, count = _sum_duration_matches(MODULE_FUZZY_RE, text)

        if total is None:
            total, count = _try_next_data_sum(kind, soup)

        if total is not None and count > 0:
            return total, "modules", count

    if kind in {"specialization", "professional_certificate"}:
        total, count = _sum_duration_matches(COURSE_STRICT_RE, text)
        if total is None:
            total, count = _sum_duration_matches(COURSE_FUZZY_RE, text)

        if total is None:
            total, count = _try_next_data_sum(kind, soup)

        if total is not None and count > 0:
            return total, "courses", count

    # fallback: "X hours to complete"
    m = GENERIC_HOURS_TO_COMPLETE_RE.search(text)
    if m:
        return float(m.group(1)), "hint", None

    return None, None, None


# ----------------------------
# Public API
# ----------------------------
def get_course_info(url: str) -> CourseInfo:
    # For parsing/output we use canonical. For fetching we prefer the original (keeps /home/welcome).
    fetch_url = sanitize_fetch_url(url)
    canonical = canonicalize_url(url) or url
    kind = detect_kind(url) or "unknown"

    # Try fetch original first (better chance to see full syllabus)
    html = None
    try:
        if fetch_url:
            html = fetch_html(fetch_url)
    except Exception:
        html = None

    # Fallback to canonical marketing page
    try:
        if html is None:
            html = fetch_html(canonical)
        soup = BeautifulSoup(html, "lxml")

        title = parse_title(soup)
        desc = parse_description(soup)

        txt = soup.get_text(" ", strip=True)
        workload_hint = parse_workload_hint(txt)

        course_count = None
        if kind in {"specialization", "professional_certificate"}:
            course_count = parse_course_count(txt)

        total, basis, items = parse_total_hours(kind, soup)

        return CourseInfo(
            url=canonical,
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
        return CourseInfo(url=canonical, kind=kind)
