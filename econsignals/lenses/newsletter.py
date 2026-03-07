"""EconSignals daily newsletter — HTML email generation and delivery.

Generates a concise, hyperlinked newsletter with three sections:
  1. Deadlines & Funding (highest priority)
  2. Top Papers
  3. Social Feed Highlights

Email delivery via Resend (https://resend.com) — just two env vars:
    RESEND_API_KEY          Your Resend API key
    ECONSIGNALS_EMAIL_TO    Recipient email

Usage:
    python lenses/newsletter.py [--days 1] [--no-send] [--email user@example.com]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from econsignals.lib.db import (
    get_db,
    get_recent_papers,
    get_upcoming_deadlines,
    update_paper_relevance,
    init_db,
)
from econsignals.lib.zotero_embeddings import compute_zotero_embedding_scores
from econsignals.lib.zotero_profile import (
    load_zotero_profile,
    load_zotero_corpus,
    zotero_author_score,
    zotero_content_score,
)

try:
    _SSL_CTX = ssl.create_default_context()
except ssl.SSLError:
    _SSL_CTX = ssl._create_unverified_context()
try:
    import certifi
    _SSL_CTX.load_verify_locations(certifi.where())
except (ImportError, Exception):
    try:
        urlopen("https://api.resend.com", timeout=5, context=_SSL_CTX)
    except (ssl.SSLCertVerificationError, ssl.SSLError):
        _SSL_CTX = ssl._create_unverified_context()
    except Exception:
        pass

PROJ_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Load .env if present (no dependencies needed)
# ---------------------------------------------------------------------------

_env_path = PROJ_ROOT / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key and val and key not in os.environ:
            os.environ[key] = val

# ---------------------------------------------------------------------------
# Styling constants (inline CSS for email client compatibility)
# ---------------------------------------------------------------------------

_BODY = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
    "max-width:640px;margin:0 auto;padding:16px;color:#1a1a1a;background:#f5f5f5;"
    "line-height:1.5"
)
_CARD = "background:white;padding:24px;border-radius:8px"
_H1 = "font-size:22px;margin:0 0 2px 0;color:#1a1a1a"
_SUB = "color:#888;font-size:12px;margin:0 0 20px 0"
_SECTION_DEADLINES = (
    "font-size:15px;color:#c62828;margin:0 0 10px 0;"
    "border-bottom:2px solid #c62828;padding-bottom:4px"
)
_SECTION_PAPERS = (
    "font-size:15px;color:#1565c0;margin:20px 0 10px 0;"
    "border-bottom:2px solid #1565c0;padding-bottom:4px"
)
_SECTION_SOCIAL = (
    "font-size:15px;color:#2e7d32;margin:20px 0 10px 0;"
    "border-bottom:2px solid #2e7d32;padding-bottom:4px"
)
_ITEM = "margin-bottom:12px;padding:6px 0;border-bottom:1px solid #eee"
_LINK = "color:#1565c0;text-decoration:none"
_META = "color:#777;font-size:12px"
_SNIPPET = "color:#444;font-size:13px"
_URGENT = "color:#c62828;font-weight:bold;font-size:13px"
_FOOTER = "color:#999;font-size:11px;margin-top:20px;text-align:center"

_TRUSTED_SOURCES = {
    "nber", "semantic_scholar", "repec_nep", "worldbank", "imf",
    "iza", "bread", "igc", "jpal_sa", "india_think_tanks", "rbi", "mospi",
}

_BLOCKED_DOI_PREFIXES = ("10.5281/", "10.17632/", "10.6084/")

_ECON_RE = re.compile(
    r"\b(econom(?:ic|ics)|development|poverty|inequality|labor|labour|"
    r"employment|wage|inflation|gdp|productivity|tax|fiscal|monetary|trade|"
    r"urban|housing|migration|education|health economics|microfinance|"
    r"randomized|rct|firm|household|consumption|prices|policy)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _e(text: str) -> str:
    return escape(str(text or ""), quote=True)


def _snippet(text: str | None, n: int = 160) -> str:
    if not text:
        return ""
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[:n].rstrip() + "..."


def _paper_url(paper: dict) -> str:
    url = (paper.get("url") or "").strip()
    if url:
        return url
    doi = (paper.get("doi") or "").strip()
    return f"https://doi.org/{doi}" if doi else ""


def _author_str(paper: dict, max_n: int = 3) -> str:
    rows = paper.get("authors") or []
    names = []
    for r in rows:
        if isinstance(r, dict):
            n = (r.get("name") or "").strip()
            if n:
                names.append(n)
    if not names:
        return "Unknown"
    if len(names) <= max_n:
        return ", ".join(names)
    return ", ".join(names[:max_n]) + f" +{len(names) - max_n}"


def _primary_source(db, paper_id: int) -> str:
    row = db.execute(
        "SELECT source FROM paper_sources WHERE paper_id = ? LIMIT 1",
        (paper_id,),
    ).fetchone()
    return (row["source"] if row else "unknown").strip().lower()


def _is_econ_paper(paper: dict) -> bool:
    doi = (paper.get("doi") or "").lower()
    if any(doi.startswith(p) for p in _BLOCKED_DOI_PREFIXES):
        return False
    text = f"{paper.get('title', '')} {paper.get('abstract', '')}"
    jel = paper.get("jel_codes") or []
    if isinstance(jel, list) and jel:
        return True
    return bool(_ECON_RE.search(text))


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------


def _gather_deadlines(days: int = 60) -> list[dict]:
    deadlines = get_upcoming_deadlines(days=days)
    today = date.today()
    result = []
    for dl in deadlines:
        dd = dl.get("deadline_date")
        if not dd:
            continue
        try:
            days_left = (date.fromisoformat(dd) - today).days
        except ValueError:
            continue
        if days_left < 0:
            continue
        dl["_days_left"] = days_left
        result.append(dl)
    result.sort(key=lambda d: d["_days_left"])
    return result


def _gather_papers(
    days: int = 1,
    limit: int = 8,
    profile: dict | None = None,
    corpus: list[dict] | None = None,
) -> list[dict]:
    db = get_db()
    try:
        fetch_limit = max(limit * 10, 150)
        papers = get_recent_papers(days=days, limit=fetch_limit)

        if len(papers) < 20 and days < 7:
            papers = get_recent_papers(days=7, limit=fetch_limit)

        curated: list[dict] = []
        for p in papers:
            if not _is_econ_paper(p):
                continue
            src = _primary_source(db, p["id"])
            p["_source"] = src
            curated.append(p)

        # Embedding-based Zotero boost (zotero-arxiv-daily style) when corpus available
        if corpus and len(corpus) > 0:
            emb_scores = compute_zotero_embedding_scores(curated, corpus)
            for p, score in zip(curated, emb_scores):
                base = float(p.get("relevance_score") or 0)
                source_boost = 0.15 if p.get("_source") in _TRUSTED_SOURCES else 0
                zotero_boost = min(score / 10.0, 0.7)  # scale to ~0–0.7
                p["_rank"] = base + source_boost + zotero_boost
                p["_zotero_boost"] = zotero_boost
        else:
            # Fallback: lexical author + content overlap
            for p in curated:
                base = float(p.get("relevance_score") or 0)
                source_boost = 0.15 if p.get("_source") in _TRUSTED_SOURCES else 0
                zotero_boost = 0.0
                if profile and profile.get("ok"):
                    title = str(p.get("title") or "")
                    abstract = str(p.get("abstract") or "")
                    author_names = [
                        (r.get("name") or "").strip()
                        for r in (p.get("authors") or [])
                        if isinstance(r, dict) and (r.get("name") or "").strip()
                    ]
                    a_score, _ = zotero_author_score(author_names, profile)
                    c_score, _ = zotero_content_score(title, abstract, profile)
                    zotero_boost = 0.4 * a_score + 0.3 * c_score
                p["_rank"] = base + source_boost + zotero_boost
                p["_zotero_boost"] = zotero_boost

        curated.sort(key=lambda p: p["_rank"], reverse=True)
        return curated[:limit]
    finally:
        db.close()


def _gather_social(days: int = 1, limit: int = 10) -> list[dict]:
    """Gather social items with source diversity (mix of twitter, mastodon, etc.)."""
    db = get_db()
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(days, 7))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        sources = [
            r["source"] for r in
            db.execute("SELECT DISTINCT source FROM social_items").fetchall()
        ]
        if not sources:
            return []

        per_source = max(limit // len(sources), 3)
        result: list[dict] = []
        seen: set[str] = set()

        for src in sources:
            rows = db.execute("""
                SELECT * FROM social_items
                WHERE source = ? AND published_at >= ?
                ORDER BY published_at DESC LIMIT ?
            """, (src, cutoff, per_source * 2)).fetchall()

            for row in rows:
                item = dict(row)
                content_key = (item.get("content") or "")[:80].strip().lower()
                if content_key in seen:
                    continue
                seen.add(content_key)
                result.append(item)
                if sum(1 for r in result if r.get("source") == src) >= per_source:
                    break

        result.sort(key=lambda x: x.get("published_at") or "", reverse=True)
        return result[:limit]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _render_deadline(dl: dict) -> str:
    name = _e(dl.get("name") or "Unnamed")
    org = _e(dl.get("organization") or "")
    days_left = dl.get("_days_left", 0)
    due = _e(dl.get("deadline_date") or "")
    url = dl.get("url") or ""
    desc = _snippet(dl.get("description"), 100)

    urgency = ""
    if days_left <= 3:
        urgency = "FINAL"
    elif days_left <= 7:
        urgency = "URGENT"
    elif days_left <= 14:
        urgency = "SOON"

    parts = [f'<div style="{_ITEM}">']
    parts.append(f'<strong style="font-size:14px">{name}</strong>')
    if org:
        parts.append(f' <span style="{_META}">({org})</span>')
    parts.append(f' <span style="{_URGENT}"> &mdash; {days_left} days</span>')
    if urgency:
        parts.append(f' <span style="{_URGENT}">[{urgency}]</span>')
    parts.append("<br>")
    parts.append(f'<span style="{_META}">Due: {due}</span>')
    if url:
        parts.append(f' <a href="{_e(url)}" style="{_LINK};font-size:12px"> Apply &rarr;</a>')
    if desc:
        parts.append(f'<br><span style="{_SNIPPET}"><em>{_e(desc)}</em></span>')
    parts.append("</div>")
    return "".join(parts)


def _render_paper(rank: int, paper: dict) -> str:
    title = _e(paper.get("title") or "Untitled")
    url = _paper_url(paper)
    authors = _e(_author_str(paper))
    source = _e(paper.get("_source") or "unknown")
    pub_date = _e((paper.get("published_at") or "")[:10])
    abstract = _e(_snippet(paper.get("abstract"), 140))
    score = float(paper.get("relevance_score") or 0)

    parts = [f'<div style="{_ITEM}">']
    if url:
        parts.append(
            f'{rank}. <a href="{_e(url)}" style="{_LINK};font-size:14px;font-weight:500">'
            f"{title}</a>"
        )
    else:
        parts.append(f'{rank}. <strong style="font-size:14px">{title}</strong>')
    parts.append(f' <span style="{_META}">({score:.2f})</span>')
    parts.append("<br>")
    parts.append(f'<span style="{_META}">{authors} &middot; {source} &middot; {pub_date}</span>')
    if abstract:
        parts.append(f'<br><span style="{_SNIPPET}">{abstract}</span>')
    parts.append("</div>")
    return "".join(parts)


def _render_social(item: dict) -> str:
    handle = _e(item.get("author_handle") or "unknown")
    source = _e(item.get("source") or "")
    content = _e(_snippet(item.get("content"), 180))
    url = item.get("url") or ""

    parts = [f'<div style="{_ITEM}">']
    parts.append(f'<strong style="font-size:13px">{handle}</strong>')
    parts.append(f' <span style="{_META}">on {source}</span>')
    parts.append(f'<br><span style="{_SNIPPET}">{content}</span>')
    if url:
        parts.append(f' <a href="{_e(url)}" style="{_LINK};font-size:12px">View &rarr;</a>')
    parts.append("</div>")
    return "".join(parts)


def generate_html(
    deadlines: list[dict],
    papers: list[dict],
    social: list[dict],
) -> str:
    today_str = date.today().strftime("%A, %B %d, %Y")

    dl_html = "".join(_render_deadline(dl) for dl in deadlines)
    if not dl_html:
        dl_html = f'<p style="{_META}">No upcoming deadlines.</p>'

    paper_html = "".join(
        _render_paper(i, p) for i, p in enumerate(papers, 1)
    )
    if not paper_html:
        paper_html = f'<p style="{_META}">No new papers today.</p>'

    social_html = "".join(_render_social(s) for s in social)
    if not social_html:
        social_html = f'<p style="{_META}">No social highlights.</p>'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="{_BODY}">
<div style="{_CARD}">
<h1 style="{_H1}">EconSignals Daily</h1>
<p style="{_SUB}">{_e(today_str)}</p>

<h2 style="{_SECTION_DEADLINES}">DEADLINES &amp; FUNDING</h2>
{dl_html}

<h2 style="{_SECTION_PAPERS}">TOP PAPERS</h2>
{paper_html}

<h2 style="{_SECTION_SOCIAL}">SOCIAL FEED</h2>
{social_html}

<p style="{_FOOTER}">
Generated by EconSignals
</p>
</div>
</body>
</html>"""


def generate_plain(
    deadlines: list[dict],
    papers: list[dict],
    social: list[dict],
) -> str:
    today_str = date.today().isoformat()
    lines = [f"ECONSIGNALS DAILY — {today_str}", "=" * 40, ""]

    lines.append("DEADLINES & FUNDING")
    lines.append("-" * 20)
    if deadlines:
        for dl in deadlines:
            name = dl.get("name") or "Unnamed"
            org = dl.get("organization") or ""
            days_left = dl.get("_days_left", 0)
            due = dl.get("deadline_date") or ""
            url = dl.get("url") or ""
            org_str = f" ({org})" if org else ""
            lines.append(f"* {name}{org_str} -- {days_left} days (due {due})")
            if url:
                lines.append(f"  {url}")
    else:
        lines.append("No upcoming deadlines.")
    lines.append("")

    lines.append("TOP PAPERS")
    lines.append("-" * 20)
    if papers:
        for i, p in enumerate(papers, 1):
            title = p.get("title") or "Untitled"
            url = _paper_url(p)
            authors = _author_str(p)
            lines.append(f"{i}. {title}")
            lines.append(f"   {authors}")
            if url:
                lines.append(f"   {url}")
    else:
        lines.append("No new papers today.")
    lines.append("")

    lines.append("SOCIAL FEED")
    lines.append("-" * 20)
    if social:
        for s in social:
            handle = s.get("author_handle") or "unknown"
            source = s.get("source") or ""
            content = _snippet(s.get("content"), 140)
            url = s.get("url") or ""
            lines.append(f"* {handle} on {source}: {content}")
            if url:
                lines.append(f"  {url}")
    else:
        lines.append("No social highlights.")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email delivery — tries Resend API first, then Gmail SMTP
#
# Option A (Resend): set RESEND_API_KEY + ECONSIGNALS_EMAIL_TO
# Option B (Gmail):  set GMAIL_APP_PASSWORD + ECONSIGNALS_EMAIL_TO
#   Generate app password: Google Account > Security > 2-Step Verification > App passwords
# ---------------------------------------------------------------------------


def _send_resend(html: str, plain: str, to: str) -> bool:
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if not api_key:
        return False

    payload = {
        "from": "EconSignals <onboarding@resend.dev>",
        "to": [to],
        "subject": f"EconSignals Daily — {date.today().isoformat()}",
        "html": html,
        "text": plain,
    }

    req = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            result = json.loads(body)
            print(f"[newsletter] email sent via Resend to {to} (id={result.get('id', '?')})", file=sys.stderr)
            return True
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        print(f"[newsletter] Resend error {exc.code}: {body}", file=sys.stderr)
        return False
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f"[newsletter] Resend failed: {exc}", file=sys.stderr)
        return False


def _send_gmail(html: str, plain: str, to: str) -> bool:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    app_pass = (os.environ.get("GMAIL_APP_PASSWORD") or "").strip()
    if not app_pass:
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"EconSignals Daily — {date.today().isoformat()}"
    msg["From"] = to
    msg["To"] = to
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(to, app_pass)
            server.send_message(msg)
        print(f"[newsletter] email sent via Gmail to {to}", file=sys.stderr)
        return True
    except Exception as exc:
        print(f"[newsletter] Gmail SMTP failed: {exc}", file=sys.stderr)
        return False


def send_email(html: str, plain: str, to_addr: str | None = None) -> bool:
    """Send newsletter — tries Resend, then Gmail SMTP. Returns True on success."""
    to = (to_addr or os.environ.get("ECONSIGNALS_EMAIL_TO") or "").strip()
    if not to:
        print("[newsletter] ECONSIGNALS_EMAIL_TO not set — skipping email", file=sys.stderr)
        return False

    if _send_resend(html, plain, to):
        return True

    if _send_gmail(html, plain, to):
        return True

    print("[newsletter] no working email method — set RESEND_API_KEY or GMAIL_APP_PASSWORD", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _update_scores_from_zotero(papers: list[dict], alpha: float = 0.05) -> int:
    """EMA update: blend Zotero affinity signal back into relevance_score.

    Papers that score well against the Zotero profile get a small upward
    nudge to their stored relevance_score, so future rankings self-improve
    as the Zotero library evolves.
    """
    updated = 0
    for p in papers:
        zb = float(p.get("_zotero_boost") or 0)
        if zb <= 0:
            continue
        old = float(p.get("relevance_score") or 0)
        target = min(old + zb, 1.0)
        new = old + alpha * (target - old)
        new = max(0.0, min(1.0, round(new, 6)))
        if abs(new - old) > 0.001:
            update_paper_relevance(p["id"], new)
            updated += 1
    return updated


def run_newsletter(
    days: int = 1,
    no_send: bool = False,
    email_to: str | None = None,
) -> dict:
    profile = load_zotero_profile()
    if not profile.get("ok"):
        print(f"[newsletter] zotero profile not loaded: {profile.get('error', '?')}", file=sys.stderr)
        profile = {}

    corpus = load_zotero_corpus()
    if corpus:
        print(f"[newsletter] using embedding-based Zotero boost ({len(corpus)} corpus items)", file=sys.stderr)
    else:
        print("[newsletter] no Zotero corpus — falling back to lexical boost", file=sys.stderr)

    deadlines = _gather_deadlines(days=60)
    papers = _gather_papers(days=days, limit=8, profile=profile, corpus=corpus)
    social = _gather_social(days=days, limit=8)

    ema_updated = _update_scores_from_zotero(papers)
    if ema_updated:
        print(f"[newsletter] EMA updated relevance_score for {ema_updated} papers", file=sys.stderr)

    html = generate_html(deadlines, papers, social)
    plain = generate_plain(deadlines, papers, social)

    today_str = date.today().isoformat()
    report_dir = PROJ_ROOT / "reports" / today_str
    report_dir.mkdir(parents=True, exist_ok=True)

    html_path = report_dir / "newsletter.html"
    html_path.write_text(html, encoding="utf-8")

    plain_path = report_dir / "newsletter.txt"
    plain_path.write_text(plain, encoding="utf-8")

    emailed = False
    if not no_send:
        emailed = send_email(html, plain, to_addr=email_to)

    return {
        "status": "ok",
        "date": today_str,
        "deadlines": len(deadlines),
        "papers": len(papers),
        "social": len(social),
        "html_path": str(html_path),
        "plain_path": str(plain_path),
        "emailed": emailed,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EconSignals daily newsletter.")
    parser.add_argument("--days", type=int, default=1, help="Paper lookback window.")
    parser.add_argument("--no-send", action="store_true", help="Skip email, just generate files.")
    parser.add_argument("--email", default="", help="Override recipient email address.")
    args = parser.parse_args()

    init_db()
    result = run_newsletter(
        days=args.days,
        no_send=args.no_send,
        email_to=args.email or None,
    )
    print(json.dumps(result))
