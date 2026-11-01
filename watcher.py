#!/usr/bin/env python3
"""DBATU website watcher (Phase 1).

Scrapes the configured DBATU pages, finds document links (PDF/DOC/XLS...),
compares them with state/seen.json and sends a Telegram alert for anything
new or silently replaced. The first run for a page only records a baseline.

Usage:
    python watcher.py              # normal run (sends alerts, writes state)
    python watcher.py --dry-run    # fetch + print only; no alerts, no state write
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state" / "seen.json"
DOWNLOAD_DIR = ROOT / "downloads"

IST = timezone(timedelta(hours=5, minutes=30))
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 dbatu-watcher/1.0 (personal student notifier)"
)
DOC_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip")
TG_MAX_FILE = 45 * 1024 * 1024  # Telegram bot upload limit is 50 MB
POLITE_DELAY = 0.3  # seconds between requests to the university server


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def fetch_html(session: requests.Session, url: str, tries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            last = e
            time.sleep(2 * attempt)
    raise RuntimeError(f"could not fetch {url}: {last}")


def file_sig(session: requests.Session, url: str) -> str:
    """Cheap fingerprint of a remote file (Last-Modified + size).

    Lets us notice when the university replaces a PDF under the SAME url.
    Returns "" if the server gives no usable headers.
    """
    for method in ("HEAD", "GET"):
        try:
            r = session.request(method, url, timeout=30, allow_redirects=True, stream=True)
            try:
                if r.status_code >= 400:
                    continue
                lm = r.headers.get("Last-Modified", "")
                cl = r.headers.get("Content-Length", "")
                return f"{lm}|{cl}" if (lm or cl) else ""
            finally:
                r.close()
        except requests.RequestException:
            continue
    return ""


def download(session: requests.Session, url: str) -> Path | None:
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    name = unquote(Path(urlparse(url).path).name) or "file.pdf"
    dest = DOWNLOAD_DIR / name
    try:
        with session.get(url, timeout=120, stream=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        return dest
    except requests.RequestException as e:
        print(f"  ! download failed for {url}: {e}")
        return None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def clean_title(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    t = re.sub(r"^PDF(?=\S)", "", t)  # site renders "PDF" badge glued to the text
    return t.rstrip("↗ ").strip()


def strip_chrome(soup: BeautifulSoup) -> None:
    """Remove menus/header/footer so nav links never count as 'new documents'."""
    for tag in soup(["script", "style", "noscript", "header", "nav", "footer", "aside"]):
        tag.decompose()
    selectors = [
        '[class*="elementor-location-header"]',
        '[class*="elementor-location-footer"]',
        '[class*="menu"]',
        '[class*="quick-links"]',
        '[id*="menu"]',
    ]
    for el in soup.select(",".join(selectors)):
        if el.parent is not None:  # skip nodes already removed with an ancestor
            el.decompose()


def _matches(pattern: str | None, *texts: str) -> bool:
    return bool(pattern) and any(re.search(pattern, t or "") for t in texts)


def extract_links(html_text: str, base_url: str, page_cfg: dict) -> tuple[dict, list[str]]:
    """Return ({doc_url: {title, section}}, [subpage urls to follow])."""
    soup = BeautifulSoup(html_text, "html.parser")
    strip_chrome(soup)

    include = page_cfg.get("include_regex")
    exclude = page_cfg.get("exclude_regex")
    follow = page_cfg.get("follow_pages_regex")

    docs: dict[str, dict] = {}
    subpages: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        title = clean_title(a.get_text(" ", strip=True)) or Path(urlparse(url).path).name
        heading = a.find_previous(["h2", "h3", "h4", "h5"])
        section = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)) if heading else ""

        if urlparse(url).path.lower().endswith(DOC_EXT):
            if include and not _matches(include, url, title, section):
                continue
            if exclude and _matches(exclude, url, title):
                continue
            docs.setdefault(url, {"title": title, "section": section})
        elif follow and _matches(follow, url) and urlparse(url).netloc.endswith("dbatu.ac.in"):
            if url not in subpages and url != base_url:
                subpages.append(url)

    return docs, subpages


def scrape_page(session: requests.Session, page_cfg: dict) -> dict[str, dict]:
    base = page_cfg["url"]
    docs, subpages = extract_links(fetch_html(session, base), base, page_cfg)

    for sub in subpages[: page_cfg.get("max_subpages", 25)]:
        time.sleep(POLITE_DELAY)
        try:
            sub_docs, _ = extract_links(fetch_html(session, sub), sub, page_cfg)
        except RuntimeError as e:
            print(f"  ! subpage skipped: {e}")
            continue
        for u, meta in sub_docs.items():
            meta["section"] = meta["section"] or sub
            docs.setdefault(u, meta)
    return docs


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"pages": {}, "last_run": None}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Notifications (Telegram and/or Discord - any channel with credentials is used)
#
# All messages are written once in a tiny HTML subset (<b>, <i>, <a href>) and each
# channel converts them to its own format.
# --------------------------------------------------------------------------
DISCORD_MAX_FILE = 8 * 1024 * 1024  # safe for default servers; larger files fall back to link-only
DISCORD_LIMIT = 1900                # Discord hard limit is 2000 chars per message


def split_message(text: str, limit: int = 3900) -> list[str]:
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def html_to_discord(s: str) -> str:
    """Convert our tiny HTML subset to Discord markdown (escaping stray markdown chars)."""

    def esc(t: str) -> str:
        return re.sub(r"([\\*_~`|>])", r"\\\1", html.unescape(t))

    def esc_label(t: str) -> str:
        return esc(t).replace("[", "\\[").replace("]", "\\]")

    tag = re.compile(r'<a href="([^"]*)">(.*?)</a>|<(/?)(b|i)>', re.S)
    out, pos = [], 0
    for m in tag.finditer(s):
        out.append(esc(s[pos:m.start()]))
        if m.group(1) is not None:
            url = html.unescape(m.group(1)).replace(")", "%29").replace(" ", "%20")
            out.append(f"[{esc_label(m.group(2))}]({url})")
        else:
            out.append("**" if m.group(4) == "b" else "*")
        pos = m.end()
    out.append(esc(s[pos:]))
    return "".join(out)


class TelegramChannel:
    name = "telegram"

    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat = os.getenv("TELEGRAM_CHAT_ID", "")

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat)

    def _api(self, method: str, **kw) -> None:
        r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", timeout=60, **kw)
        if not r.ok:
            print(f"! telegram {method} failed: {r.status_code} {r.text[:200]}")

    def send_text(self, message: str, ping: bool = False) -> None:
        for chunk in split_message(message):
            self._api(
                "sendMessage",
                data={
                    "chat_id": self.chat,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )

    def send_file(self, path: Path, caption: str) -> None:
        if path.stat().st_size > TG_MAX_FILE:
            print(f"  (telegram: {path.name} too large to attach, link only)")
            return
        with open(path, "rb") as f:
            self._api(
                "sendDocument",
                data={"chat_id": self.chat, "caption": caption[:1000], "parse_mode": "HTML"},
                files={"document": (path.name, f)},
            )


class DiscordChannel:
    name = "discord"

    def __init__(self) -> None:
        self.url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        # Optional, only prepended to real alerts: "@everyone", "<@&ROLE_ID>" or "<@USER_ID>"
        self.mention = os.getenv("DISCORD_MENTION", "").strip()
        if self.url and not re.match(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/", self.url):
            print("! DISCORD_WEBHOOK_URL does not look like a Discord webhook URL")

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def _post(self, make_kwargs) -> None:
        """POST with simple handling of Discord's rate limit (HTTP 429)."""
        for _ in range(4):
            r = requests.post(self.url, timeout=60, **make_kwargs())
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                time.sleep(min(wait, 10) + 0.2)
                continue
            if not r.ok:
                print(f"! discord post failed: {r.status_code} {r.text[:200]}")
            return
        print("! discord: gave up after repeated rate limiting")

    def send_text(self, message: str, ping: bool = False) -> None:
        md = html_to_discord(message)
        if ping and self.mention:
            md = f"{self.mention}\n{md}"
        for chunk in split_message(md, DISCORD_LIMIT):
            payload = {
                "content": chunk.strip(),
                "username": "DBATU Watcher",
                "flags": 4,  # SUPPRESS_EMBEDS: no giant link previews
            }
            self._post(lambda p=payload: {"json": p})
            time.sleep(0.5)

    def send_file(self, path: Path, caption: str) -> None:
        if path.stat().st_size > DISCORD_MAX_FILE:
            print(f"  (discord: {path.name} too large to attach, link only)")
            return
        payload = {
            "content": html_to_discord(caption)[:DISCORD_LIMIT],
            "username": "DBATU Watcher",
            "flags": 4,
        }

        def make():
            return {
                "data": {"payload_json": json.dumps(payload)},
                "files": {"files[0]": (path.name, open(path, "rb"))},
            }

        self._post(make)
        time.sleep(0.5)


class Notifier:
    def __init__(self, enabled: bool):
        self.channels = [c for c in (TelegramChannel(), DiscordChannel()) if c.configured]
        self.live = enabled and bool(self.channels)
        if enabled and not self.channels:
            print("! no notification channel configured (set DISCORD_WEBHOOK_URL and/or "
                  "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) - printing instead of sending")
        elif self.live:
            print("notifying via: " + ", ".join(c.name for c in self.channels))

    def text(self, message: str, ping: bool = False) -> None:
        print("\n--- message ---\n" + re.sub(r"<[^>]+>", "", html.unescape(message)))
        if self.live:
            for c in self.channels:
                c.send_text(message, ping=ping)

    def document(self, path: Path, caption: str) -> None:
        if not self.live:
            print(f"(would attach {path.name})")
            return
        for c in self.channels:
            c.send_file(path, caption)


def fmt_entry(url: str, meta: dict, relevant: bool) -> str:
    star = "⭐ " if relevant else "• "
    sec = f" <i>({html.escape(meta['section'])})</i>" if meta.get("section") else ""
    return f'{star}<a href="{html.escape(url, quote=True)}">{html.escape(meta["title"])}</a>{sec}'


# --------------------------------------------------------------------------
# Main logic
# --------------------------------------------------------------------------
def check_page(session, page_cfg: dict, state: dict, notifier: Notifier, dry: bool) -> str | None:
    """Process one page. Returns a one-line status string."""
    name = page_cfg["name"]
    print(f"\n== {name} ==")
    docs = scrape_page(session, page_cfg)
    print(f"  found {len(docs)} document links")
    if not docs:
        raise RuntimeError(
            f"{name}: 0 document links found - page layout may have changed or the page is down"
        )

    pstate = state["pages"].setdefault(name, {"url": page_cfg["url"], "links": {}, "initialized": False})
    known: dict = pstate["links"]
    relevant_re = page_cfg.get("relevant_regex")
    now = datetime.now(IST).strftime("%Y-%m-%d")

    new_urls = [u for u in docs if u not in known]
    changed_urls: list[str] = []

    # Fingerprint files: all new ones + (optionally) existing ones to catch silent replacements.
    check_existing = page_cfg.get("detect_replaced_files", True) and pstate["initialized"]
    sigs: dict[str, str] = {}
    for u in docs:
        if u in new_urls or check_existing:
            sigs[u] = file_sig(session, u)
            time.sleep(POLITE_DELAY)
    if check_existing:
        for u in docs:
            if u in known and sigs.get(u) and known[u].get("sig") and sigs[u] != known[u]["sig"]:
                changed_urls.append(u)

    # ---- baseline run: record silently ----
    if not pstate["initialized"]:
        for u, meta in docs.items():
            known[u] = {**meta, "sig": sigs.get(u, ""), "first_seen": now}
        pstate["initialized"] = True
        return f"{name}: baseline saved ({len(docs)} links)"

    # ---- build alert ----
    def is_rel(u: str) -> bool:
        m = docs[u]
        return _matches(relevant_re, u, m["title"], m["section"])

    new_sorted = sorted(new_urls, key=lambda u: (not is_rel(u), docs[u]["title"]))
    changed_sorted = sorted(changed_urls, key=lambda u: (not is_rel(u), docs[u]["title"]))

    if new_sorted or changed_sorted:
        lines = []
        page_link = f'<a href="{html.escape(page_cfg["url"], quote=True)}">{html.escape(name)}</a>'
        if new_sorted:
            lines.append(f"🆕 <b>{len(new_sorted)} new on {page_link}</b>")
            lines += [fmt_entry(u, docs[u], is_rel(u)) for u in new_sorted[:30]]
            if len(new_sorted) > 30:
                lines.append(f"…and {len(new_sorted) - 30} more")
        if changed_sorted:
            lines.append(f"\n♻️ <b>{len(changed_sorted)} file(s) replaced on {page_link}</b>")
            lines += [fmt_entry(u, docs[u], is_rel(u)) for u in changed_sorted[:30]]
        lines.append("\n⭐ = matches your relevance filter")
        notifier.text("\n".join(lines), ping=True)

        # attach the relevant PDFs themselves (max 5 per page per run)
        attach = [u for u in new_sorted + changed_sorted if is_rel(u)][:5]
        if attach and not dry:
            for u in attach:
                path = download(session, u)
                if path:
                    notifier.document(path, html.escape(docs[u]["title"]))

    # ---- update state ----
    for u in new_urls:
        known[u] = {**docs[u], "sig": sigs.get(u, ""), "first_seen": now}
    for u in changed_urls:
        known[u]["sig"] = sigs[u]
        known[u]["replaced_on"] = now
    for u in docs:  # backfill a missing signature without alerting
        if u in known and not known[u].get("sig") and sigs.get(u):
            known[u]["sig"] = sigs[u]

    return f"{name}: {len(new_urls)} new, {len(changed_urls)} replaced"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print only; no alerts, no state write")
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    state = load_state()
    notifier = Notifier(enabled=not args.dry_run)
    session = make_session()

    summaries, errors = [], []
    for page_cfg in cfg["pages"]:
        try:
            summaries.append(check_page(session, page_cfg, state, notifier, args.dry_run))
        except Exception as e:  # keep going - one broken page must not block the rest
            traceback.print_exc()
            errors.append(f"{page_cfg['name']}: {e}")

    if errors:
        notifier.text(
            "⚠️ <b>DBATU watcher problem</b>\n" + "\n".join(html.escape(e) for e in errors)
        )

    print("\n== summary ==")
    for s in summaries:
        print(" ", s)
    for e in errors:
        print("  ERROR", e)

    if not args.dry_run:
        state["last_run"] = datetime.now(IST).isoformat(timespec="seconds")
        save_state(state)
        baselines = [s for s in summaries if "baseline" in s]
        if baselines:
            notifier.text("✅ <b>DBATU watcher set up</b>\n" + "\n".join(html.escape(b) for b in baselines))
    return 1 if (errors and not summaries) else 0


if __name__ == "__main__":
    sys.exit(main())
