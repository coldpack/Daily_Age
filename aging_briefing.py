#!/usr/bin/env python3
"""
Daily Aging Research Briefing
=============================

Collects newly published papers from a configured list of aging / gerontology /
geroscience journals, sorts every title alphabetically into a single A-Z list,
renders it as a PDF, and emails that PDF to you.

Data source
-----------
Crossref's public REST API (https://api.crossref.org). Every journal in
journals.json deposits its metadata there, so this replaces sixteen brittle
HTML scrapers with one stable, documented, publisher-sanctioned interface.

Usage
-----
    python aging_briefing.py                 # normal daily run
    python aging_briefing.py --dry-run       # fetch + build PDF, do not email or save state
    python aging_briefing.py --sample        # build a PDF from built-in fixture data (no network)
    python aging_briefing.py --check-issns   # verify each ISSN resolves to the right journal
    python aging_briefing.py --lookback 7    # widen the window for a first/backfill run

Configuration
-------------
    journals.json   journals, ISSNs, behaviour settings
    .env            SMTP credentials (see .env.example)

Dependencies: requests, reportlab
"""

from __future__ import annotations

import argparse
import ctypes
import html
import io
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable

import requests
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
)

__version__ = "1.0.0"

HERE = Path(__file__).resolve().parent
CROSSREF_BASE = "https://api.crossref.org"
USER_AGENT_TEMPLATE = (
    "AgingResearchDailyBriefing/{version} (https://example.org; mailto:{mailto})"
)

log = logging.getLogger("briefing")


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass
class Paper:
    doi: str
    title: str
    journal: str
    authors: str = ""
    published: str = ""
    url: str = ""

    def sort_key(self, ignore_leading_articles: bool = False) -> tuple:
        return (normalized_sort_key(self.title, ignore_leading_articles), self.journal)


@dataclass
class RunStats:
    journals_queried: int = 0
    journals_failed: list[str] = field(default_factory=list)
    raw_results: int = 0
    filtered_noise: int = 0
    filtered_old: int = 0
    already_seen: int = 0
    new_papers: int = 0


# --------------------------------------------------------------------------
# Small helpers: env, config, state
# --------------------------------------------------------------------------


def load_dotenv(path: Path) -> None:
    """Minimal .env loader so the tool has no python-dotenv dependency."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        # Real environment variables win over the file.
        os.environ.setdefault(key, value)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Config file {path} is not valid JSON: {exc}") from exc
    cfg.setdefault("settings", {})
    cfg.setdefault("journals", [])
    cfg.setdefault("noise_title_patterns", [])
    if not cfg["journals"]:
        raise SystemExit(f"No journals configured in {path}")
    return cfg


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen": {}, "last_run": None, "last_success": None}
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        log.warning("State file %s was corrupt; starting fresh.", path)
        return {"seen": {}, "last_run": None, "last_success": None}
    state.setdefault("seen", {})
    state.setdefault("last_run", None)
    state.setdefault("last_success", None)
    return state


def save_state(path: Path, state: dict[str, Any], retention_days: int) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).date().isoformat()
    state["seen"] = {doi: seen for doi, seen in state["seen"].items() if seen >= cutoff}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)  # atomic, so an interrupted run can't shred the DOI history


# --------------------------------------------------------------------------
# Text cleaning and sorting
# --------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)

# Crossref titles arrive as JATS-flavoured markup; strip it to plain text.
def repair_mojibake(text: str) -> str:
    """Undo UTF-8 that a publisher already mis-decoded as cp1252 before deposit.

    Crossref serves exactly what was deposited, so 'Alzheimer\u2019s' sometimes
    arrives as 'Alzheimerâ€™s'. Round-tripping through cp1252 recovers it. Only
    attempted when the tell-tale sequences are present, and reverted if the
    round-trip fails.
    """
    if "â€" not in text and "Ã" not in text and "â€™" not in text:
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def clean_title(raw: str) -> str:
    text = repair_mojibake(raw or "")
    text = html.unescape(text)
    text = html.unescape(text)  # some records are double-escaped
    text = TAG_RE.sub("", text)
    text = WS_RE.sub(" ", text).strip()
    return text


def normalized_sort_key(title: str, ignore_leading_articles: bool = False) -> str:
    """Case- and accent-insensitive key so 'Épigenetic' files next to 'Epigenetic'."""
    text = unicodedata.normalize("NFKD", title)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    # Punctuation and non-Latin glyphs are ignored for filing purposes, so
    # '"Successful" ageing' files with 'Successful ageing', and 'β-amyloid'
    # files under A for 'amyloid' — the usual library/index convention.
    text = re.sub(r"[^0-9a-z ]+", " ", text)
    text = WS_RE.sub(" ", text).strip()
    if ignore_leading_articles:
        text = LEADING_ARTICLE_RE.sub("", text)
    return text


def first_letter(title: str, ignore_leading_articles: bool = False) -> str:
    key = normalized_sort_key(title, ignore_leading_articles)
    if not key:
        return "#"
    return key[0].upper() if key[0].isalpha() else "#"


def compile_noise_patterns(patterns: Iterable[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def is_noise(title: str, patterns: list[re.Pattern]) -> bool:
    if not title or len(title) < 5:
        return True
    return any(p.search(title) for p in patterns)


def format_authors(author_list: list[dict] | None, max_names: int = 3) -> str:
    if not author_list:
        return ""
    names = []
    for a in author_list:
        family = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        if family and given:
            names.append(f"{given[0]}. {family}")
        elif family:
            names.append(family)
        elif a.get("name"):
            names.append(a["name"].strip())
    if not names:
        return ""
    if len(names) > max_names:
        return ", ".join(names[:max_names]) + ", et al."
    return ", ".join(names)


def extract_date(work: dict) -> str:
    for key in ("published", "published-online", "published-print", "issued", "created"):
        parts = (work.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            nums = [n for n in parts[0] if isinstance(n, int)]
            if len(nums) >= 3:
                return f"{nums[0]:04d}-{nums[1]:02d}-{nums[2]:02d}"
            if len(nums) == 2:
                return f"{nums[0]:04d}-{nums[1]:02d}"
            return f"{nums[0]:04d}"
    return ""


# --------------------------------------------------------------------------
# Crossref client
# --------------------------------------------------------------------------

SELECT_FIELDS = "DOI,title,container-title,author,issued,published,URL,type"


def make_session(contact_email: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT_TEMPLATE.format(
                version=__version__, mailto=contact_email
            ),
            "Accept": "application/json",
        }
    )
    return session


def crossref_get(
    session: requests.Session,
    url: str,
    params: dict,
    timeout: int,
    attempts: int = 3,
) -> dict:
    """GET with backoff. Crossref rate-limits and occasionally 5xxs under load."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 400 and "select" in params:
                # Some Crossref deployments reject certain select fields; retry plainly.
                log.debug("select rejected for %s; retrying without it", url)
                params = {k: v for k, v in params.items() if k != "select"}
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = min(8, 2 ** attempt)
                log.warning(
                    "HTTP %s from Crossref (attempt %d/%d); retrying in %ds",
                    resp.status_code, attempt, attempts, wait,
                )
                time.sleep(wait)
                last_error = RuntimeError(f"HTTP {resp.status_code}")
                continue
            resp.raise_for_status()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
            wait = min(8, 2 ** attempt)
            log.warning(
                "Request error (%s) attempt %d/%d; retrying in %ds",
                exc, attempt, attempts, wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"Crossref request failed after {attempts} attempts: {last_error}")


def wait_for_network(session: requests.Session, timeout: int = 300,
                     interval: int = 10) -> bool:
    """Poll Crossref until it answers, or until timeout.

    Modern Standby (S0) wakes the machine for a scheduled task but may keep the
    network adapter powered down for a while, or power it back down mid-run.
    A run that starts the instant the timer fires can therefore find no DNS at
    all. Waiting costs nothing when the network is already up: the first probe
    succeeds and we continue immediately.
    """
    deadline = time.time() + timeout
    attempt = 0
    while True:
        try:
            r = session.get(f"{CROSSREF_BASE}/journals/0002-0729", timeout=10)
            if r.status_code < 500:
                if attempt:
                    log.info("Network is up after %d attempt(s).", attempt + 1)
                return True
        except requests.RequestException:
            pass
        attempt += 1
        if time.time() + interval >= deadline:
            log.error("No network after %ds of waiting.", timeout)
            return False
        log.info("Network not reachable yet; waiting %ds...", interval)
        time.sleep(interval)


def fetch_journal(
    session: requests.Session,
    journal_name: str,
    issns: list[str],
    from_date: str,
    timeout: int,
    rows: int,
    date_field: str = "created",
) -> list[dict]:
    """Fetch works first registered on/after from_date for every ISSN.

    date_field is deliberately "created" (when Crossref first saw the DOI) and
    NOT "index" (when the record was last touched). Publishers re-deposit old
    records constantly to update licences, references and ORCIDs, and every
    such touch bumps the index date — so an index-date filter drags the entire
    back catalogue into a "new papers" briefing.
    """
    works: dict[str, dict] = {}
    for issn in issns:
        url = f"{CROSSREF_BASE}/journals/{issn}/works"
        cursor = "*"
        pages = 0
        while cursor and pages < 25:  # 25 * rows is a generous ceiling per journal
            params = {
                "filter": f"from-{date_field}-date:{from_date},type:journal-article",
                "rows": rows,
                "cursor": cursor,
                "select": SELECT_FIELDS,
                "sort": date_field,
                "order": "desc",
            }
            payload = crossref_get(session, url, params, timeout)
            message = payload.get("message", {})
            items = message.get("items", [])
            for item in items:
                doi = (item.get("DOI") or "").lower().strip()
                if doi:
                    works.setdefault(doi, item)
            next_cursor = message.get("next-cursor")
            if not items or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            pages += 1
            time.sleep(0.35)  # be a good API citizen
        time.sleep(0.35)
    log.info("  %-52s %3d record(s)", journal_name[:52], len(works))
    return list(works.values())


def collect_papers(
    cfg: dict, state: dict, from_date: str, stats: RunStats
) -> list[Paper]:
    settings = cfg["settings"]
    session = make_session(settings.get("contact_email", "you@example.com"))
    noise = compile_noise_patterns(cfg.get("noise_title_patterns", []))
    timeout = int(settings.get("request_timeout_seconds", 30))
    rows = int(settings.get("max_rows_per_page", 200))
    date_field = settings.get("date_filter", "created")
    max_age = int(settings.get("max_pub_age_days", 400))
    oldest_allowed = (datetime.now(timezone.utc) - timedelta(days=max_age)).date().isoformat()
    seen: dict[str, str] = state["seen"]

    papers: dict[str, Paper] = {}
    consecutive_failures = 0
    if not wait_for_network(session, timeout=int(settings.get("network_wait_seconds", 300))):
        log.error("Giving up before querying: no route to Crossref.")
        stats.journals_failed = [j["name"] for j in cfg["journals"]
                                 if j.get("enabled", True)]
        return []
    log.info("Querying Crossref for works indexed since %s", from_date)

    for entry in cfg["journals"]:
        if not entry.get("enabled", True):
            continue
        name = entry["name"]
        issns = entry.get("issns", [])
        if not issns:
            log.warning("  %s has no ISSNs configured; skipping", name)
            continue
        stats.journals_queried += 1
        try:
            items = fetch_journal(session, name, issns, from_date, timeout, rows,
                                  date_field)
        except Exception as exc:  # one bad journal must not kill the briefing
            log.error("  FAILED %s: %s", name, exc)
            stats.journals_failed.append(name)
            consecutive_failures += 1
            # Only treat repeated failures as fatal if nothing has succeeded.
            # If journals have already returned data, the network was demonstrably
            # up moments ago, so this is a transient drop -- push on and retry.
            if consecutive_failures >= 3 and not papers:
                remaining = [j["name"] for j in cfg["journals"]
                             if j.get("enabled", True)
                             and j["name"] not in stats.journals_failed
                             and j["name"] != name]
                log.error(
                    "Three journals failed in a row — treating this as a "
                    "connectivity problem rather than retrying %d more. No "
                    "state will be recorded, so the next run covers this "
                    "window again.", len(remaining),
                )
                stats.journals_failed.extend(remaining)
                break
            continue

        consecutive_failures = 0

        for item in items:
            stats.raw_results += 1
            doi = (item.get("DOI") or "").lower().strip()
            titles = item.get("title") or []
            title = clean_title(titles[0]) if titles else ""
            if not doi or not title:
                stats.filtered_noise += 1
                continue
            if is_noise(title, noise):
                stats.filtered_noise += 1
                continue
            if doi in seen:
                stats.already_seen += 1
                continue
            if doi in papers:
                continue
            published = extract_date(item)
            # Belt and braces: even if a stale record slips past the date
            # filter, a publication date from years ago is not "new".
            if published and len(published) >= 4 and published[:10] < oldest_allowed[:len(published[:10])]:
                stats.filtered_old += 1
                continue
            papers[doi] = Paper(
                doi=doi,
                title=title,
                journal=name,
                authors=format_authors(item.get("author")),
                published=published,
                url=item.get("URL") or f"https://doi.org/{doi}",
            )

    # Second pass: anything that failed while the adapter was down.
    if stats.journals_failed and papers:
        retry_names = list(stats.journals_failed)
        issn_map = {j["name"]: j.get("issns", []) for j in cfg["journals"]}
        log.info("Retrying %d journal(s) that failed mid-run...", len(retry_names))
        if wait_for_network(session, timeout=240):
            for name in retry_names:
                try:
                    items = fetch_journal(session, name, issn_map.get(name, []),
                                          from_date, timeout, rows, date_field)
                except Exception as exc:
                    log.error("  still failing: %s (%s)", name, exc)
                    continue
                stats.journals_failed.remove(name)
                for item in items:
                    stats.raw_results += 1
                    doi = (item.get("DOI") or "").lower().strip()
                    titles = item.get("title") or []
                    title = clean_title(titles[0]) if titles else ""
                    if not doi or not title or is_noise(title, noise):
                        stats.filtered_noise += 1
                        continue
                    if doi in seen or doi in papers:
                        stats.already_seen += 1
                        continue
                    published = extract_date(item)
                    if published and published[:10] < oldest_allowed[:len(published[:10])]:
                        stats.filtered_old += 1
                        continue
                    papers[doi] = Paper(
                        doi=doi, title=title, journal=name,
                        authors=format_authors(item.get("author")),
                        published=published,
                        url=item.get("URL") or f"https://doi.org/{doi}",
                    )

    stats.new_papers = len(papers)
    ordered = sorted(
        papers.values(),
        key=lambda p: p.sort_key(settings.get("ignore_leading_articles", False)),
    )
    return ordered


# --------------------------------------------------------------------------
# Fonts: journal titles are full of Greek letters, which Helvetica lacks
# --------------------------------------------------------------------------

def _font_candidates() -> list[tuple[str, str]]:
    """(regular, bold) pairs to try, in order. First existing pair wins.

    Windows is not always installed on C:, and user-installed fonts live under
    LOCALAPPDATA rather than the system folder, so both are resolved from the
    environment instead of being hardcoded.
    """
    win = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or "C:\\Windows"
    win_fonts = Path(win) / "Fonts"
    user_fonts = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"

    pairs = [
        # Linux
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
        # macOS
        ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
         "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ]
    # Windows: Segoe UI first (it is the better-looking face), then Arial.
    for folder in (win_fonts, user_fonts):
        pairs.extend([
            (str(folder / "segoeui.ttf"), str(folder / "segoeuib.ttf")),
            (str(folder / "arial.ttf"), str(folder / "arialbd.ttf")),
            (str(folder / "calibri.ttf"), str(folder / "calibrib.ttf")),
            (str(folder / "DejaVuSans.ttf"), str(folder / "DejaVuSans-Bold.ttf")),
        ])
    return pairs


# Fallback used only when no Unicode TTF is available.
LATIN1_MAP = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
    "ζ": "zeta", "η": "eta", "θ": "theta", "ι": "iota", "κ": "kappa",
    "λ": "lambda", "μ": "mu", "ν": "nu", "ξ": "xi", "π": "pi", "ρ": "rho",
    "σ": "sigma", "τ": "tau", "υ": "upsilon", "φ": "phi", "χ": "chi",
    "ψ": "psi", "ω": "omega", "Α": "Alpha", "Β": "Beta", "Γ": "Gamma",
    "Δ": "Delta", "Θ": "Theta", "Λ": "Lambda", "Σ": "Sigma", "Φ": "Phi",
    "Ω": "Omega",
    "–": "-", "—": "-", "‐": "-", "‑": "-", "−": "-",
    "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
    "≤": "<=", "≥": ">=", "≈": "~", "×": "x", "→": "->", "↑": "up",
    "↓": "down", "′": "'", "″": '"', "•": "-", "™": "(TM)",
}


def _serif_candidates() -> list[tuple[str, str]]:
    """Serif display faces for the masthead, best-looking first.

    Palatino Linotype and Constantia both ship with Windows and read as
    letterhead rather than word-processor default; Times New Roman is the
    universal floor. Every path is existence-checked, so a missing face just
    falls through to the next.
    """
    win = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or "C:\\Windows"
    wf = Path(win) / "Fonts"
    uf = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"

    pairs: list[tuple[str, str]] = []
    for folder in (wf, uf):
        pairs.extend([
            (str(folder / "pala.ttf"), str(folder / "palab.ttf")),        # Palatino
            (str(folder / "GARA.TTF"), str(folder / "GARABD.TTF")),       # Garamond
            (str(folder / "constan.ttf"), str(folder / "constanb.ttf")),  # Constantia
            (str(folder / "georgia.ttf"), str(folder / "georgiab.ttf")),  # Georgia
            (str(folder / "times.ttf"), str(folder / "timesbd.ttf")),     # Times
        ])
    pairs.extend([
        # macOS
        ("/System/Library/Fonts/Supplemental/Georgia.ttf",
         "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"),
        ("/System/Library/Fonts/Supplemental/Times New Roman.ttf",
         "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf"),
        # Linux
        ("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"),
        ("/usr/share/fonts/dejavu/DejaVuSerif.ttf",
         "/usr/share/fonts/dejavu/DejaVuSerif-Bold.ttf"),
    ])
    return pairs


def setup_serif() -> tuple[str, str, bool]:
    """Register a serif display face. Returns (regular, bold, is_unicode).

    Falls back to reportlab's built-in Times, which needs no font file at all
    and so can never be missing — the masthead is short and Latin, so the
    narrower character set costs nothing.
    """
    for regular, bold in _serif_candidates():
        rp, bp = Path(regular), Path(bold)
        if not rp.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("BriefingSerif", str(rp)))
            if bp.exists():
                pdfmetrics.registerFont(TTFont("BriefingSerif-Bold", str(bp)))
                bold_name = "BriefingSerif-Bold"
            else:
                bold_name = "BriefingSerif"
            pdfmetrics.registerFontFamily(
                "BriefingSerif", normal="BriefingSerif", bold=bold_name,
                italic="BriefingSerif", boldItalic=bold_name,
            )
            log.debug("Masthead face: %s", rp)
            return "BriefingSerif", bold_name, True
        except Exception as exc:
            log.debug("Could not register serif %s: %s", rp, exc)
    log.debug("No serif TTF found; using built-in Times for the masthead.")
    return "Times-Roman", "Times-Bold", False


def setup_fonts() -> tuple[str, str, bool]:
    """Register a Unicode TTF if one exists. Returns (regular, bold, is_unicode)."""
    for regular, bold in _font_candidates():
        rp, bp = Path(regular), Path(bold)
        if not rp.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("BriefingFont", str(rp)))
            if bp.exists():
                pdfmetrics.registerFont(TTFont("BriefingFont-Bold", str(bp)))
                bold_name = "BriefingFont-Bold"
            else:
                bold_name = "BriefingFont"
            pdfmetrics.registerFontFamily(
                "BriefingFont", normal="BriefingFont", bold=bold_name,
                italic="BriefingFont", boldItalic=bold_name,
            )
            log.debug("Using font %s", rp)
            return "BriefingFont", bold_name, True
        except Exception as exc:
            log.debug("Could not register %s: %s", rp, exc)
    log.warning(
        "No Unicode TTF found; falling back to Helvetica and transliterating "
        "Greek/math characters (β -> beta, etc.)."
    )
    return "Helvetica", "Helvetica-Bold", False


def downgrade_glyphs(text: str, unicode_ok: bool) -> str:
    """Rewrite characters the active font cannot render. No XML escaping."""
    if unicode_ok:
        return text
    for a, b in LATIN1_MAP.items():
        text = text.replace(a, b)
    text = unicodedata.normalize("NFKD", text)
    # "ignore", not "replace": replace emits '?' for every dropped glyph, and
    # stripping those would also delete genuine question marks.
    return text.encode("latin-1", "ignore").decode("latin-1")


def to_pdf_text(text: str, unicode_ok: bool) -> str:
    """Downgrade glyphs, then escape for reportlab's mini-XML (Paragraphs only).

    Do not use this for text drawn straight onto the canvas — canvas strings
    are literal, so escaping would print "&amp;" instead of "&".
    """
    text = downgrade_glyphs(text, unicode_ok)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# PDF rendering — single page, auto-fitting
# --------------------------------------------------------------------------

INK = colors.HexColor("#111827")   # near-black for titles
ACCENT = colors.HexColor("#0E3A5C")  # deep navy for structure
ACCENT_LT = colors.HexColor("#3E7CA6")
LINK = colors.HexColor("#16324A")   # linked titles
MUTED = colors.HexColor("#71808F")  # metadata
RULE = colors.HexColor("#DCE3EA")   # hairlines
WASH = colors.HexColor("#F2F6F9")   # header band

PAGE_W, PAGE_H = LETTER
MARGIN = 0.52 * inch
HEADER_H = 1.27 * inch    # reserved at the top, drawn straight onto the canvas
FOOTER_H = 0.46 * inch


@dataclass
class Density:
    """One rung on the ladder from generous to tightly packed."""
    columns: int
    font_size: float
    leading: float
    meta_size: float
    space_after: float
    show_letters: bool
    show_authors: bool
    show_date: bool
    title_limit: int | None = None


# Tried in order; the first one that fits on a single page wins.
DENSITY_LADDER = [
    Density(1, 11.0, 14.2, 8.0, 8.0, True, True,  True),
    Density(1, 10.0, 13.0, 7.5, 6.5, True, True,  True),
    Density(1, 9.2,  11.8, 7.1, 5.0, True, True,  True),
    Density(2, 8.6,  10.6, 6.7, 5.0, True, True,  True),
    Density(2, 8.2,  10.0, 6.4, 4.2, True, True,  True),
    Density(2, 7.8,  9.4,  6.2, 3.6, True, False, True),
    Density(2, 7.4,  8.9,  6.0, 3.0, True, False, True),
    Density(2, 7.0,  8.4,  5.8, 2.6, True, False, True),
    Density(3, 6.8,  8.0,  5.6, 2.4, True, False, True),
    Density(3, 6.4,  7.6,  5.4, 2.1, True, False, False),
    Density(3, 6.0,  7.1,  5.2, 1.8, True, False, False),
    Density(3, 5.7,  6.8,  5.0, 1.6, True, False, False, title_limit=150),
    Density(3, 5.4,  6.5,  4.8, 1.4, True, False, False, title_limit=120),
]


def _truncate(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def _column_frames(columns: int) -> list[Frame]:
    gutter = 16 if columns == 2 else 13
    usable = PAGE_W - 2 * MARGIN - gutter * (columns - 1)
    col_w = usable / columns
    top = PAGE_H - HEADER_H
    height = top - FOOTER_H
    frames = []
    for i in range(columns):
        x = MARGIN + i * (col_w + gutter)
        frames.append(
            Frame(x, FOOTER_H, col_w, height, id=f"col{i}",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                  showBoundary=0)
        )
    return frames


def _tracked(canvas, x, y, text, font, size, color, tracking=0.0,
             align="left") -> float:
    """Draw letter-spaced text. Canvas has no tracking; text objects do."""
    width = canvas.stringWidth(text, font, size) + tracking * max(len(text) - 1, 0)
    if align == "right":
        x -= width
    t = canvas.beginText(x, y)
    t.setFont(font, size)
    t.setCharSpace(tracking)
    t.setFillColor(color)
    t.textOut(text)
    canvas.drawText(t)
    return width


def _draw_furniture(canvas, doc, ctx: dict) -> None:
    """Masthead and footer are painted straight onto the canvas, not flowed.

    That buys precise control over letter-spacing and rules, and keeps the
    full column height free for titles.
    """
    regular, bold, unicode_ok = ctx["fonts"]
    s_reg, s_bold, s_uni = ctx["serif"]
    canvas.saveState()

    # --- masthead --------------------------------------------------------
    canvas.setFillColor(WASH)
    canvas.rect(0, PAGE_H - HEADER_H + 8, PAGE_W, HEADER_H - 8, stroke=0, fill=1)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, PAGE_H - 4.5, PAGE_W, 4.5, stroke=0, fill=1)  # accent bleed

    # Serif caps, widely tracked: the letterhead convention.
    _tracked(canvas, MARGIN, PAGE_H - 38, "AGING DAILY", s_bold, 19, ACCENT, 3.4)

    # Date sits on the same baseline, right-aligned.
    _tracked(canvas, PAGE_W - MARGIN, PAGE_H - 38, ctx["date_line"],
             s_reg, 11.5, INK, 0.6, align="right")
    _tracked(canvas, PAGE_W - MARGIN, PAGE_H - 51, ctx["window_line"],
             regular, 7.0, MUTED, 0, align="right")

    # Double rule -- heavy over light -- the classic letterhead device.
    canvas.setStrokeColor(ACCENT)
    canvas.setLineWidth(1.1)
    canvas.line(MARGIN, PAGE_H - 58, PAGE_W - MARGIN, PAGE_H - 58)
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, PAGE_H - 61.5, PAGE_W - MARGIN, PAGE_H - 61.5)

    # --- stat strip ------------------------------------------------------
    strip_y = PAGE_H - 77
    x = MARGIN
    for i, (value, label) in enumerate(ctx["stats"]):
        if i:
            canvas.setStrokeColor(RULE)
            canvas.setLineWidth(0.6)
            canvas.line(x - 13, strip_y - 2, x - 13, strip_y + 10)
        x += _tracked(canvas, x, strip_y, value, s_bold, 12.5, ACCENT, 0) + 5
        x += _tracked(canvas, x, strip_y + 1.5, label.upper(),
                      regular, 6.6, MUTED, 1.0) + 26

    _tracked(canvas, PAGE_W - MARGIN, strip_y + 1.5, ctx["strip_right"],
             regular, 6.6, MUTED, 1.0, align="right")

    # --- column dividers -------------------------------------------------
    cols = ctx["columns"]
    if cols > 1:
        gutter = 16 if cols == 2 else 13
        col_w = (PAGE_W - 2 * MARGIN - gutter * (cols - 1)) / cols
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        for i in range(1, cols):
            cx = MARGIN + i * col_w + (i - 0.5) * gutter
            canvas.line(cx, FOOTER_H - 6, cx, PAGE_H - HEADER_H - 6)

    # --- footer ----------------------------------------------------------
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.6)
    canvas.line(MARGIN, FOOTER_H - 13, PAGE_W - MARGIN, FOOTER_H - 13)
    _tracked(canvas, MARGIN, FOOTER_H - 24, ctx["footer_left"],
             regular, 6.4, MUTED, 0)
    _tracked(canvas, PAGE_W - MARGIN, FOOTER_H - 24, ctx["footer_right"],
             regular, 6.4, MUTED, 0, align="right")
    canvas.restoreState()


def _build_story(papers: list[Paper], d: Density, fonts, ignore_articles: bool,
                 omitted: int, tail: list[tuple[str, int]] | None = None,
                 group_by: str = "journal") -> list[Any]:
    regular, bold, unicode_ok = fonts
    T = lambda s: to_pdf_text(s, unicode_ok)  # noqa: E731

    entry_style = ParagraphStyle(
        "entry", fontName=regular, fontSize=d.font_size, leading=d.leading,
        textColor=INK, spaceAfter=d.space_after, allowWidows=0, allowOrphans=0,
    )
    letter_style = ParagraphStyle(
        "letter", fontName=bold, fontSize=d.font_size + 0.6,
        leading=d.leading + 2, textColor=ACCENT_LT,
        spaceBefore=d.space_after + 3, spaceAfter=2.5,
    )
    # Journal headings carry more text than a single letter, so they get a
    # slightly smaller size and tighter tracking to stay on one line.
    journal_style = ParagraphStyle(
        "journalhead", fontName=bold, fontSize=d.font_size + 0.1,
        leading=d.leading + 1.5, textColor=ACCENT,
        spaceBefore=d.space_after + 4, spaceAfter=2.5,
    )
    counts: dict[str, int] = {}
    for _p in papers:
        counts[_p.journal] = counts.get(_p.journal, 0) + 1
    note_style = ParagraphStyle(
        "note", fontName=regular, fontSize=d.meta_size + 0.4,
        leading=d.leading, textColor=MUTED, spaceBefore=6,
    )

    story: list[Any] = []
    current_group = None

    for idx, p in enumerate(papers, start=1):
        block: list[Any] = []
        if d.show_letters:
            if group_by == "journal":
                group = p.journal
                if group != current_group:
                    current_group = group
                    n = counts.get(group, 0)
                    head = (f"{T(group).upper()}"
                            f'  <font size="{d.meta_size:.1f}" color="#71808F">{n}</font>')
                    block.append(Paragraph(head, journal_style))
                    block.append(HRFlowable(width="100%", thickness=0.5,
                                            color=RULE, spaceAfter=1.5))
            else:
                group = first_letter(p.title, ignore_articles)
                if group != current_group:
                    current_group = group
                    block.append(Paragraph(T(group), letter_style))
                    block.append(HRFlowable(width="100%", thickness=0.5,
                                            color=RULE, spaceAfter=1.5))

        href = (p.url or f"https://doi.org/{p.doi}").replace("&", "&amp;")
        title = T(_truncate(p.title, d.title_limit))
        # The whole title is the link target — one generous click area.
        number = (f'<font size="{d.meta_size:.1f}" color="#9AA7B4">{idx}</font>&nbsp;')
        line = (
            f'{number}<link href="{href}" color="#16324A">{title}</link>'
        )

        meta_bits = [] if (group_by == "journal" and d.show_letters) else [T(p.journal).upper()]
        if d.show_date and p.published:
            meta_bits.append(T(p.published))
        if d.show_authors and p.authors:
            meta_bits.append(T(p.authors))
        meta = " · ".join(meta_bits)
        if not meta:
            meta = "&nbsp;"
        line += (
            f'<br/><font size="{d.meta_size:.1f}" color="#71808F">{meta}</font>'
        )

        block.append(Paragraph(line, entry_style))
        story.append(KeepTogether(block))

    if omitted:
        story.append(Paragraph(
            T(f"+ {omitted} further title{'s' if omitted != 1 else ''} — "
              f"the complete list is in the body of this email."),
            note_style,
        ))

    if tail:
        # Only rendered when the day's list leaves room for it.
        story.append(Spacer(1, 10))
        story.append(HRFlowable(width="100%", thickness=0.6, color=RULE,
                                spaceAfter=6))
        label = ParagraphStyle(
            "tail_label", fontName=bold, fontSize=d.meta_size,
            leading=d.meta_size + 3, textColor=ACCENT_LT, spaceAfter=3,
        )
        body = ParagraphStyle(
            "tail_body", fontName=regular, fontSize=d.meta_size + 0.3,
            leading=d.meta_size + 4.5, textColor=MUTED,
        )
        story.append(Paragraph(T("CONTRIBUTING JOURNALS"), label))
        parts = [
            f'{T(name).upper()}&nbsp;<font color="#0E3A5C">{count}</font>'
            for name, count in tail
        ]
        story.append(Paragraph(" · ".join(parts), body))
    return story


def _render(papers: list[Paper], d: Density, ctx: dict, target,
            omitted: int, tail: list[tuple[str, int]] | None = None) -> int:
    """Render one attempt. Returns the number of pages produced."""
    ctx = dict(ctx, columns=d.columns)
    pages = [0]

    doc = BaseDocTemplate(
        target, pagesize=LETTER,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=HEADER_H, bottomMargin=FOOTER_H,
        title=ctx["pdf_title"], author="Daily Aging Research Briefing",
        subject="New papers in aging, gerontology and geroscience journals",
        creator="aging_briefing.py",
    )

    def on_page(canvas, doc_):
        pages[0] = max(pages[0], doc_.page)
        _draw_furniture(canvas, doc_, ctx)

    doc.addPageTemplates([
        PageTemplate(id="cols", frames=_column_frames(d.columns), onPage=on_page)
    ])
    doc.build(_build_story(papers, d, ctx["fonts"], ctx["ignore_articles"],
                           omitted, tail, ctx.get("group_by", "journal")))
    return pages[0]


def build_pdf(
    papers: list[Paper],
    out_path: Path,
    run_date: datetime,
    window_start: str,
    stats: RunStats,
    settings: dict,
) -> Path:
    """Render the briefing onto exactly one page.

    Strategy: walk down a ladder of layout densities, rendering each into
    memory until one fits a single page. If even the tightest layout
    overflows — an unusually heavy publication day — trim titles from the
    tail until it fits, and say so on the page. The email body always
    carries the complete list, so nothing is actually lost.
    """
    fonts = setup_fonts()
    serif = setup_serif()
    regular, bold, unicode_ok = fonts
    ignore_articles = settings.get("ignore_leading_articles", False)
    group_by = settings.get("group_by", "journal")
    # One page is the design target; raise this if you would rather see every
    # title on a very heavy day than have the tail trimmed.
    max_pages = max(1, int(settings.get("max_pages", 1)))

    # Defensive: the caller sorts, but the whole promise of this document is
    # A-Z, so guarantee it here rather than trusting every future call site.
    if group_by == "journal":
        papers = sorted(papers, key=lambda p: (p.journal.casefold(),
                                               normalized_sort_key(p.title, ignore_articles)))
    else:
        papers = sorted(papers, key=lambda p: p.sort_key(ignore_articles))

    journal_counts: dict[str, int] = {}
    for p in papers:
        journal_counts[p.journal] = journal_counts.get(p.journal, 0) + 1

    footer_left = "Metadata via Crossref · titles link to the publisher's page"
    if stats.journals_failed:
        shown = ", ".join(stats.journals_failed[:2])
        extra = len(stats.journals_failed) - 2
        footer_left += f" · unreachable this run: {shown}"
        if extra > 0:
            footer_left += f" +{extra}"

    ctx = {
        "fonts": fonts,
        "serif": serif,
        "ignore_articles": ignore_articles,
        "group_by": group_by,
        # Canvas strings are literal, so these get glyph downgrading but no
        # XML escaping. %B is locale-dependent, hence the serif's charset.
        "date_line": downgrade_glyphs(run_date.strftime("%d %B %Y"), serif[2]),
        "window_line": f"indexed since {window_start}",
        "strip_right": downgrade_glyphs(
            "BY JOURNAL" if group_by == "journal" else "SORTED A \u2013 Z", unicode_ok),
        "stats": [
            (str(len(papers)), "new papers"),
            (str(len(journal_counts)), "journals"),
        ],
        "footer_left": downgrade_glyphs(footer_left, unicode_ok),
        "footer_right": "",
        "pdf_title": f"Daily Briefing — Aging Research — {run_date:%Y-%m-%d}",
        "columns": 2,
    }

    if not papers:
        _render_empty(out_path, ctx, run_date)
        log.info("PDF written (no new papers): %s", out_path)
        return out_path

    chosen: tuple[Density, int] | None = None
    for d in DENSITY_LADDER:
        buf = io.BytesIO()
        try:
            pages = _render(papers, d, ctx, buf, 0)
        except Exception as exc:
            log.debug("Density %s failed: %s", d.font_size, exc)
            continue
        log.debug("Density fs=%.1f cols=%d -> %d page(s)", d.font_size, d.columns, pages)
        if pages <= max_pages:
            chosen = (d, 0)
            break

    shown = papers
    omitted = 0
    if chosen is None:
        # Heaviest layout still overflows: trim the tail until it fits.
        d = DENSITY_LADDER[-1]
        lo, hi = 1, len(papers)
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            buf = io.BytesIO()
            pages = _render(papers[:mid], d, ctx, buf, len(papers) - mid)
            if pages <= max_pages:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        shown = papers[:best]
        omitted = len(papers) - best
        chosen = (d, omitted)
        log.warning(
            "%d papers exceed %d page(s); showing %d and noting the rest. The "
            "full list is in the email body. Raise max_pages in journals.json "
            "to keep every title.", len(papers), max_pages, best,
        )

    density, omitted = chosen
    tail = None
    if not omitted and group_by != "journal":
        candidate = sorted(journal_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        probe = io.BytesIO()
        try:
            if _render(shown, density, ctx, probe, 0, candidate) <= max_pages:
                tail = candidate
        except Exception:
            tail = None

    ctx["footer_right"] = (
        f"{len(shown)} of {len(papers)} titles" if omitted
        else f"{len(papers)} title{'s' if len(papers) != 1 else ''}"
    )
    pages = _render(shown, density, ctx, str(out_path), omitted, tail)
    log.info(
        "PDF written: %s (%d papers, %d column layout at %.1fpt, %d page)",
        out_path, len(papers), density.columns, density.font_size, pages,
    )
    return out_path


def _render_empty(out_path: Path, ctx: dict, run_date: datetime) -> None:
    regular, bold, unicode_ok = ctx["fonts"]
    ctx = dict(ctx, columns=1, footer_right="quiet day")

    doc = BaseDocTemplate(
        str(out_path), pagesize=LETTER,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=HEADER_H, bottomMargin=FOOTER_H,
        title=ctx["pdf_title"], author="Daily Aging Research Briefing",
    )
    doc.addPageTemplates([
        PageTemplate(id="one", frames=_column_frames(1),
                     onPage=lambda c, d: _draw_furniture(c, d, ctx))
    ])
    style = ParagraphStyle(
        "empty", fontName=regular, fontSize=10, leading=15,
        textColor=MUTED, alignment=TA_CENTER,
    )
    doc.build([
        Spacer(1, 2.4 * inch),
        Paragraph(to_pdf_text(
            "No new papers were indexed for the monitored journals! Have a good one!",
            unicode_ok), style),
        Spacer(1, 6),
        Paragraph(to_pdf_text(
            "Publication is uneven across journals; sometimes a day like this is normal.",
            unicode_ok),
            ParagraphStyle("empty2", parent=style, fontSize=8, textColor=RULE)),
    ])


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------


def send_email(pdf_path: Path, papers: list[Paper], run_date: datetime,
               settings: dict) -> None:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    security = os.environ.get("SMTP_SECURITY", "ssl").lower()
    sender = os.environ.get("EMAIL_FROM") or user

    def addresses(var: str) -> list[str]:
        return [a.strip() for a in (os.environ.get(var) or "").split(",") if a.strip()]

    recipients = addresses("EMAIL_TO")
    cc = addresses("EMAIL_CC")
    bcc = addresses("EMAIL_BCC")

    missing = [n for n, v in
               [("SMTP_HOST", host), ("SMTP_USER", user),
                ("SMTP_PASSWORD", password), ("EMAIL_TO", recipients)] if not v]
    if missing:
        raise SystemExit(
            "Missing email configuration: " + ", ".join(missing) +
            ". Copy .env.example to .env and fill it in, or run with --dry-run."
        )

    msg = EmailMessage()
    count = len(papers)
    msg["Subject"] = (
        f"Aging Daily: Your Regularly Scheduled Brief — {count} new aging {'paper' if count == 1 else 'papers'} "
        f"— {run_date:%Y-%m-%d}"
    )
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    if cc:
        msg["Cc"] = ", ".join(cc)
    # No Bcc header is written. A Bcc header in a sent message defeats the
    # point -- the whole hidden list would be visible to every recipient.
    # Blind delivery works by naming the addresses in the SMTP envelope only,
    # which is what to_addrs below does.

    lines = [
        f"Daily Aging Research Briefing — {run_date:%A, %d %B %Y}",
        "",
        f"{count} new {'paper' if count == 1 else 'papers'} across the monitored journals.",
        "The attached PDF lists every title alphabetically.",
        "",
    ]
    if settings.get("include_titles_in_email_body", True) and papers:
        lines.append("-" * 62)
        for i, p in enumerate(papers, 1):
            lines.append(f"{i}. {p.title}")
            lines.append(f"   {p.journal} — https://doi.org/{p.doi}")
        lines.append("-" * 62)
        lines.append("")
    lines.append("Metadata via Crossref.")
    msg.set_content("\n".join(lines))

    msg.add_attachment(
        pdf_path.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=pdf_path.name,
    )

    # The envelope carries every address; the headers carry only the visible ones.
    all_recipients = recipients + cc + bcc
    seen: set[str] = set()
    envelope = [a for a in all_recipients
                if not (a.lower() in seen or seen.add(a.lower()))]

    context = ssl.create_default_context()

    def deliver() -> None:
        if security == "ssl":
            with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as server:
                server.login(user, password)
                server.send_message(msg, from_addr=sender, to_addrs=envelope)
        else:
            with smtplib.SMTP(host, port, timeout=60) as server:
                server.ehlo()
                if security == "starttls":
                    server.starttls(context=context)
                    server.ehlo()
                server.login(user, password)
                server.send_message(msg, from_addr=sender, to_addrs=envelope)

    # The PDF is already built by this point; losing it to a network blip that
    # lasts seconds would be absurd. Retry before giving up.
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            deliver()
            last = None
            break
        except (smtplib.SMTPException, OSError) as exc:
            last = exc
            if attempt == 3:
                break
            log.warning("Send failed (%s); attempt %d/3, retrying in 30s", exc, attempt)
            time.sleep(30)
    if last is not None:
        raise last

    # Bcc addresses are counted, not printed: logging them would leak the very
    # thing the sender chose to hide.
    detail = ", ".join(recipients)
    if cc:
        detail += f" (cc {len(cc)})"
    if bcc:
        detail += f" (bcc {len(bcc)})"
    log.info("Email sent to %s", detail)


# --------------------------------------------------------------------------
# Maintenance commands
# --------------------------------------------------------------------------


def check_issns(cfg: dict) -> int:
    """Verify every configured ISSN resolves to the journal you think it does."""
    session = make_session(cfg["settings"].get("contact_email", "you@example.com"))
    timeout = int(cfg["settings"].get("request_timeout_seconds", 30))
    problems = 0
    print(f"\n{'CONFIGURED NAME':<48} {'ISSN':<11} CROSSREF TITLE")
    print("-" * 110)
    for entry in cfg["journals"]:
        for issn in entry.get("issns", []):
            try:
                payload = crossref_get(
                    session, f"{CROSSREF_BASE}/journals/{issn}", {}, timeout, attempts=2
                )
                title = payload.get("message", {}).get("title", "(no title)")
                counts = payload.get("message", {}).get("counts", {})
                total = counts.get("total-dois", "?")
                mark = " " if _titles_look_alike(entry["name"], title) else "!"
                if mark == "!":
                    problems += 1
                print(f"{mark}{entry['name'][:47]:<47} {issn:<11} {title}  ({total} DOIs)")
            except Exception as exc:
                problems += 1
                print(f"!{entry['name'][:47]:<47} {issn:<11} LOOKUP FAILED: {exc}")
            time.sleep(0.3)
    print("-" * 110)
    if problems:
        print(f"\n{problems} entr{'y' if problems == 1 else 'ies'} marked '!' — "
              "review the Crossref title and correct journals.json if it is wrong.\n")
    else:
        print("\nAll ISSNs resolved and matched their configured names.\n")
    return 0 if problems == 0 else 1


def _titles_look_alike(a: str, b: str) -> bool:
    na = set(re.findall(r"[a-z]+", a.lower())) - {"the", "of", "and", "journal", "journals", "series"}
    nb = set(re.findall(r"[a-z]+", b.lower())) - {"the", "of", "and", "journal", "journals", "series"}
    if not na or not nb:
        return False
    overlap = len(na & nb) / min(len(na), len(nb))
    return overlap >= 0.5


SAMPLE_PAPERS = [
    ("Zebrafish models of accelerated ageing reveal telomere-independent pathways",
     "Aging Cell", "10.1111/acel.sample01"),
    ("A multi-omic clock predicts biological age from a single blood draw",
     "Nature Aging", "10.1038/s43587-sample02"),
    ("Inflammaging and the gut microbiome in nonagenarians: a cohort study",
     "GeroScience", "10.1007/s11357-sample03"),
    ("Cellular senescence drives NAD+ decline in skeletal muscle",
     "The Journals of Gerontology: Series A", "10.1093/gerona/sample04"),
    ("Frailty trajectories and hospital readmission among older adults",
     "Age and Ageing", "10.1093/ageing/sample05"),
    ("β-amyloid clearance declines with age in the glymphatic system",
     "Frontiers in Aging Neuroscience", "10.3389/fnagi-sample06"),
    ("Proteostasis collapse precedes functional decline in aged neurons",
     "Mechanisms of Ageing and Development", "10.1016/j.mad.sample07"),
    ("Loneliness, social networks and cognitive decline over 12 years",
     "The Journals of Gerontology: Series B", "10.1093/geronb/sample08"),
    ("Metformin and healthspan: an updated meta-analysis of randomized trials",
     "Aging (Aging-US)", "10.18632/aging.sample09"),
    ("Caregiver burden in dementia care: a mixed-methods study",
     "The Gerontologist", "10.1093/geront/sample10"),
]


def sample_papers() -> list[Paper]:
    return [
        Paper(doi=doi, title=title, journal=journal,
              authors="A. Researcher, B. Scientist, et al.",
              published=datetime.now().strftime("%Y-%m-%d"),
              url=f"https://doi.org/{doi}")
        for title, journal, doi in SAMPLE_PAPERS
    ]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


# SetThreadExecutionState flags (winbase.h)
ES_CONTINUOUS        = 0x80000000
ES_SYSTEM_REQUIRED   = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


class KeepSystemAwake:
    """Stop Windows returning to sleep while the briefing is running.

    On a Modern Standby (S0) machine, a wake timer grants only a brief
    execution window. Windows then starts drifting back toward standby and
    powers the network adapter down -- even though the task is still running.
    That is what killed the 21:27 run: ten journals answered in ten seconds,
    then DNS stopped resolving entirely.

    SetThreadExecutionState with ES_SYSTEM_REQUIRED tells Windows the system
    must remain running. ES_AWAYMODE_REQUIRED is the flag designed for exactly
    this case: it keeps the machine working with the display off, rather than
    lighting up the room. ES_CONTINUOUS makes it persist until we clear it.

    No-op on anything other than Windows.
    """

    def __init__(self) -> None:
        self.active = False

    def __enter__(self) -> "KeepSystemAwake":
        if os.name != "nt":
            return self
        try:
            kernel32 = ctypes.windll.kernel32
            # Away mode is not supported on every machine; fall back cleanly.
            for flags, label in (
                (ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED, "away mode"),
                (ES_CONTINUOUS | ES_SYSTEM_REQUIRED, "system required"),
            ):
                if kernel32.SetThreadExecutionState(flags) != 0:
                    self.active = True
                    log.info("Holding the system awake (%s) for this run.", label)
                    return self
            log.warning("Could not request that the system stay awake.")
        except Exception as exc:
            log.warning("SetThreadExecutionState unavailable: %s", exc)
        return self

    def __exit__(self, *exc) -> None:
        if self.active and os.name == "nt":
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                log.debug("Released the system-awake request.")
            except Exception:
                pass


class AlreadyRunning(Exception):
    pass


class SingleInstance:
    """Cross-platform 'only one run at a time' guard.

    Uses O_EXCL file creation, which is atomic on both NTFS and POSIX, rather
    than flock (which does not exist on Windows). A lock left behind by a
    crashed or force-killed run goes stale after `stale_after` seconds so the
    job cannot wedge itself permanently.
    """

    def __init__(self, path: Path, stale_after: int = 6 * 3600):
        self.path = path
        self.stale_after = stale_after
        self.fd: int | None = None

    def __enter__(self) -> "SingleInstance":
        for attempt in (1, 2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, f"{os.getpid()} {datetime.now().isoformat()}\n"
                         .encode("utf-8"))
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0
                if attempt == 1 and age > self.stale_after:
                    log.warning("Removing stale lock (%.1f h old): %s",
                                age / 3600, self.path)
                    try:
                        self.path.unlink()
                        continue
                    except OSError:
                        pass
                raise AlreadyRunning(str(self.path))
        raise AlreadyRunning(str(self.path))

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        try:
            self.path.unlink()
        except OSError:
            pass
        return None


def force_utf8_streams() -> None:
    """Stop Windows from crashing on Greek letters and typographic dashes.

    When output is redirected to a file, Windows uses the legacy ANSI code page
    (cp1252), which cannot encode 'β' or '—'. Logging a journal title or the
    font-fallback warning would then raise UnicodeEncodeError and kill the run.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # Python < 3.7 or a stream that cannot be reconfigured


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Daily aging-research briefing: collect, alphabetize, PDF, email.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", type=Path, default=HERE / "journals.json")
    p.add_argument("--state", type=Path, default=HERE / "state.json")
    p.add_argument("--out-dir", type=Path, default=HERE / "briefings")
    p.add_argument("--env", type=Path, default=HERE / ".env")
    p.add_argument("--lookback", type=int, default=None,
                   help="Days to look back (overrides config; use for backfill).")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch and build the PDF, but do not email or update state.")
    p.add_argument("--no-email", action="store_true", help="Build the PDF only.")
    p.add_argument("--sample", action="store_true",
                   help="Render a PDF from built-in fixture data (no network).")
    p.add_argument("--check-issns", action="store_true",
                   help="Verify each configured ISSN against Crossref, then exit.")
    p.add_argument("--log-file", type=Path, default=None,
                   help="Also append logs here (UTF-8). Useful for Task "
                        "Scheduler, which cannot redirect output itself.")
    p.add_argument("--no-lock", action="store_true",
                   help="Skip the single-instance lock.")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    force_utf8_streams()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    load_dotenv(args.env)
    cfg = load_config(args.config)
    settings = cfg["settings"]
    run_date = datetime.now()

    # Before the output directory is touched: --check-issns writes nothing, so
    # it must work even somewhere read-only.
    if args.check_issns:
        return check_issns(cfg)

    try:
        args.out_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        log.error(
            "Cannot create '%s' — Windows protects this location and the "
            "program has no rights to write here. Move the whole folder "
            "somewhere under your user profile, e.g. C:\\Users\\<you>\\Daily_Brief, "
            "and run it again. (Program Files, Windows and the drive root all "
            "require administrator rights.)", args.out_dir,
        )
        return 1

    stats = RunStats()

    if args.sample:
        papers = sample_papers()
        papers.sort(key=lambda p: p.sort_key(settings.get("ignore_leading_articles", False)))
        stats.journals_queried = len(cfg["journals"])
        stats.new_papers = len(papers)
        window_start = (run_date - timedelta(days=1)).strftime("%Y-%m-%d")
        out = args.out_dir / f"sample-briefing-{run_date:%Y-%m-%d}.pdf"
        build_pdf(papers, out, run_date, window_start, stats, settings)
        print(f"\nSample PDF: {out}\n")
        return 0

    state = load_state(args.state)

    # Window: from the last successful run (with a grace margin for Crossref's
    # indexing lag), or lookback_days for a first run. Duplicates are impossible
    # regardless, because every DOI ever sent is remembered in state.json.
    lookback = args.lookback if args.lookback is not None else int(
        settings.get("lookback_days", 2))
    from_dt = datetime.now(timezone.utc) - timedelta(days=lookback)
    if state.get("last_success") and args.lookback is None:
        try:
            last = datetime.fromisoformat(state["last_success"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            from_dt = min(from_dt, last - timedelta(days=1))
        except ValueError:
            pass
    window_start = from_dt.date().isoformat()

    papers = collect_papers(cfg, state, window_start, stats)

    log.info(
        "Results: %d raw, %d front-matter/errata, %d too old, %d already sent, "
        "%d new", stats.raw_results, stats.filtered_noise, stats.filtered_old,
        stats.already_seen, stats.new_papers,
    )

    enabled = sum(1 for j in cfg["journals"] if j.get("enabled", True))
    if not papers and stats.journals_failed and len(stats.journals_failed) >= enabled:
        log.error(
            "Every journal failed, so this is a connectivity or configuration "
            "problem, not a quiet day. Nothing sent and no state recorded; the "
            "next run will cover the same window."
        )
        return 1

    if not papers and not settings.get("send_when_empty", True) and not args.dry_run:
        log.info("No new papers and send_when_empty is false; nothing to do.")
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state["last_success"] = state["last_run"]
        save_state(args.state, state, int(settings.get("seen_retention_days", 180)))
        return 0

    out = args.out_dir / f"The-Daily-Age-{run_date:%Y-%m-%d}.pdf"
    build_pdf(papers, out, run_date, window_start, stats, settings)

    if args.dry_run or args.no_email:
        log.info("Skipping email (%s).", "--dry-run" if args.dry_run else "--no-email")
        print(f"\nPDF: {out}\n")
        if args.dry_run:
            return 0
    else:
        send_email(out, papers, run_date, settings)

    # Only record DOIs once the briefing has actually gone out, so a failed
    # send does not silently swallow a day of papers.
    today = datetime.now(timezone.utc).date().isoformat()
    for p in papers:
        state["seen"][p.doi] = today
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    if not stats.journals_failed:
        state["last_success"] = state["last_run"]
    save_state(args.state, state, int(settings.get("seen_retention_days", 180)))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.check_issns or args.sample:
        return _run(args)
    if args.no_lock:
        with KeepSystemAwake():
            return _run(args)
    lock = args.state.parent / "briefing.lock"
    try:
        with SingleInstance(lock), KeepSystemAwake():
            return _run(args)
    except AlreadyRunning:
        force_utf8_streams()
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s  %(levelname)-7s %(message)s")
        log.warning("Another run is already in progress (%s); exiting.", lock)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
