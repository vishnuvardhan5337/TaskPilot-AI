#!/usr/bin/env python3
"""
crawler_cli.py - a small, safe, same-domain website crawler.

This is the CLI tool that TaskPilot AI invokes as a *separate process*.

    python crawler_cli.py --url https://example.com --max-pages 3 --output json

Contract (what the orchestrator relies on)
    stdout : exactly ONE JSON document - always, even on failure
    stderr : human-readable progress log
    exit   : 0 success | 2 invalid input / blocked URL | 3 failed (nothing crawled) | 4 partial

Safety built in (defence in depth - the CLI is safe even when run on its own):
    * http/https only, no embedded credentials, no unusual ports
    * host is DNS-resolved and every resulting IP must be public (blocks localhost,
      private ranges, link-local/cloud-metadata 169.254.x.x, etc.)
    * redirects are followed manually and each hop is re-validated
    * crawling never leaves the original domain
    * robots.txt is honoured
    * hard caps: pages, bytes per page, per-request timeout, total time budget
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time
from collections import deque
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = "TaskPilotBot/1.0 (educational demo crawler; honours robots.txt)"
HARD_MAX_PAGES = 10
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".gz", ".tar",
    ".mp3", ".mp4", ".avi", ".mov", ".css", ".js", ".json", ".xml", ".ico",
    ".woff", ".woff2", ".exe", ".dmg",
)
AUTH_PATH_HINTS = ("/login", "/logout", "/signin", "/sign-in", "/signup", "/sign-up",
                   "/register", "/cart", "/checkout", "/account", "/admin")
BLOCK_TAGS = ["p", "div", "li", "blockquote", "table", "ul", "ol", "section", "article", "dl"]
MAX_PARAGRAPHS = 15
PARAGRAPH_CHARS = 600
MAX_HEADINGS = 15
MAX_LINKS_LISTED = 15

EXIT_OK, EXIT_INVALID, EXIT_FAILED, EXIT_PARTIAL = 0, 2, 3, 4


class BlockedURL(Exception):
    """URL refused by the safety checks."""


class FetchError(Exception):
    """A page could not be fetched or parsed."""


def log(msg: str) -> None:
    print(f"[crawler] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- safety
def _ip_is_public(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


def validate_url(url: str) -> str:
    """Return the URL if it is safe to request, otherwise raise BlockedURL."""
    if not isinstance(url, str) or not url.strip():
        raise BlockedURL("empty URL")
    url = url.strip()
    if len(url) > 2048:
        raise BlockedURL("URL is too long")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedURL(f"unsupported scheme {parsed.scheme!r}: only http and https are allowed")
    if not parsed.hostname:
        raise BlockedURL("URL has no host name")
    if parsed.username or parsed.password:
        raise BlockedURL("URLs with embedded credentials are not allowed")
    try:
        port = parsed.port
    except ValueError:
        raise BlockedURL("invalid port number")
    if port not in ALLOWED_PORTS:
        raise BlockedURL(f"port {port} is not allowed (use 80, 443, 8080 or 8443)")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise BlockedURL(f"host {host!r} is an internal name and is blocked")
    try:
        infos = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise BlockedURL(f"cannot resolve host {host!r}: {exc}")
    addresses = {info[4][0] for info in infos}
    for addr in addresses:
        if not _ip_is_public(addr):
            raise BlockedURL(
                f"host {host!r} resolves to non-public address {addr}; "
                "private/internal destinations are blocked"
            )
    return url


def trim(text: str, limit: int) -> str:
    """Cut at a word boundary instead of mid-word."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def url_key(url: str):
    """Identity of a page: ignores www, http/https, fragment and trailing slash."""
    parts = urlparse(url)
    return (site_key(parts.hostname), parts.path.rstrip("/") or "/", parts.query)


def site_key(hostname: str | None) -> str:
    host = (hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


# --------------------------------------------------------------------------- fetching
def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    return session


def fetch(session, url, site, timeout, validator):
    """GET with manual, re-validated redirects. Returns (response, final_url)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        validator(current)
        if site_key(urlparse(current).hostname) != site:
            raise BlockedURL(f"{urlparse(current).hostname} is outside the original domain")
        try:
            resp = session.get(current, timeout=timeout, allow_redirects=False, stream=True)
        except requests.exceptions.Timeout:
            raise FetchError("request timed out")
        except requests.exceptions.SSLError:
            raise FetchError("TLS/SSL certificate error")
        except requests.exceptions.ConnectionError:
            raise FetchError("could not connect to the site")
        except requests.exceptions.RequestException as exc:
            raise FetchError(f"request failed ({type(exc).__name__})")
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            nxt = urljoin(current, resp.headers["Location"])
            resp.close()
            log(f"  redirect -> {nxt}")
            current = nxt
            continue
        return resp, current
    raise FetchError("too many redirects")


def read_body(resp) -> bytes:
    ctype = resp.headers.get("Content-Type", "").lower()
    if ctype and "html" not in ctype and "text/plain" not in ctype:
        resp.close()
        raise FetchError(f"not an HTML page (Content-Type: {ctype.split(';')[0]})")
    buf = bytearray()
    try:
        for chunk in resp.iter_content(65536):
            buf.extend(chunk)
            if len(buf) > MAX_BYTES:
                log("  page truncated at 2 MB")
                break
    except requests.exceptions.RequestException as exc:
        raise FetchError(f"download failed ({type(exc).__name__})")
    finally:
        resp.close()
    return bytes(buf)


class Robots:
    """Conservative robots.txt handling."""

    def __init__(self, allow_all: bool, parser=None, note: str = ""):
        self.allow_all, self.parser, self.note = allow_all, parser, note

    def allowed(self, url: str) -> bool:
        if self.parser is None:
            return self.allow_all
        return self.parser.can_fetch(USER_AGENT, url)


def load_robots(session, start_url, site, timeout, validator) -> Robots:
    robots_url = urljoin(start_url, "/robots.txt")
    try:
        resp, _ = fetch(session, robots_url, site, timeout, validator)
    except (BlockedURL, FetchError) as exc:
        log(f"robots.txt unreachable ({exc}) -> treating site as disallowed")
        return Robots(False, note=f"robots.txt unreachable ({exc}); crawl refused to stay polite")
    code = resp.status_code
    if code == 200:
        text = read_body_text(resp)
        parser = robotparser.RobotFileParser()
        parser.parse(text.splitlines())
        log("robots.txt loaded")
        return Robots(True, parser, note="robots.txt honoured")
    resp.close()
    if code in (401, 403) or code >= 500:
        log(f"robots.txt returned HTTP {code} -> treating site as disallowed")
        return Robots(False, note=f"robots.txt returned HTTP {code}; crawl refused")
    log(f"no robots.txt (HTTP {code}) -> all paths allowed")
    return Robots(True, note=f"no robots.txt (HTTP {code})")


def read_body_text(resp) -> str:
    try:
        data = b"".join(resp.iter_content(65536))[:MAX_BYTES]
    except requests.exceptions.RequestException:
        data = b""
    finally:
        resp.close()
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- extraction
def clean(text: str) -> str:
    return " ".join(text.split())


def extract(html: bytes, page_url: str, site: str):
    """Parse one HTML page. Returns (page_fields, internal_links)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
        tag.decompose()

    title = clean(soup.title.get_text()) if soup.title else ""

    description = ""
    for meta in soup.find_all("meta"):
        key = (meta.get("name") or meta.get("property") or "").lower()
        if key in ("description", "og:description") and meta.get("content"):
            description = clean(meta["content"])
            break

    headings = []
    for h in soup.find_all(["h1", "h2", "h3"]):
        text = clean(h.get_text(" "))
        if text:
            headings.append({"level": h.name, "text": text[:200]})
        if len(headings) >= MAX_HEADINGS:
            break

    paragraphs, seen_p = [], set()
    for p in soup.find_all("p"):
        text = clean(p.get_text(" "))
        if len(text) >= 25 and text not in seen_p:
            seen_p.add(text)
            paragraphs.append(trim(text, PARAGRAPH_CHARS))
        if len(paragraphs) >= MAX_PARAGRAPHS:
            break

    if len(paragraphs) < 5:  # many sites put their text in span/div/li/blockquote instead of <p>
        for el in soup.find_all(["blockquote", "li", "dd", "td", "span", "div", "article"]):
            if len(paragraphs) >= MAX_PARAGRAPHS:
                break
            if el.find(BLOCK_TAGS) is not None:  # only leaf-level blocks, avoids duplicated nested text
                continue
            text = clean(el.get_text(" "))
            if len(text) >= 40 and text not in seen_p:
                seen_p.add(text)
                paragraphs.append(trim(text, PARAGRAPH_CHARS))

    internal, seen_l, external = [], set(), 0
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urldefrag(urljoin(page_url, href))[0]
        parts = urlparse(absolute)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        if parts.path.lower().endswith(SKIP_EXTENSIONS):
            continue
        if any(hint in parts.path.lower() for hint in AUTH_PATH_HINTS):
            continue  # never follow login/cart/account style pages
        if site_key(parts.hostname) != site:
            external += 1
            continue
        if absolute not in seen_l:
            seen_l.add(absolute)
            internal.append(absolute)

    fields = {
        "title": title,
        "description": description,
        "headings": headings,
        "paragraphs": paragraphs,
        "word_count": len(soup.get_text(" ").split()),
        "internal_link_count": len(internal),
        "external_link_count": external,
        "internal_links": internal[:MAX_LINKS_LISTED],
    }
    return fields, internal


# --------------------------------------------------------------------------- crawl
def _blank_page(url: str) -> dict:
    return {"url": url, "status": "error", "http_status": None, "title": "", "description": "",
            "headings": [], "paragraphs": [], "word_count": 0, "internal_link_count": 0,
            "external_link_count": 0, "internal_links": [], "error": None}


def crawl(start_url, max_pages, timeout=10, max_seconds=90, delay=0.5,
          session=None, validator=validate_url) -> dict:
    started = time.monotonic()
    session = session or make_session()
    start_url = validator(start_url)
    site = site_key(urlparse(start_url).hostname)
    log(f"start: {start_url} (max {max_pages} pages, same domain only)")

    robots = load_robots(session, start_url, site, timeout, validator)
    queue, queued = deque([start_url]), {url_key(start_url)}
    visited_final: set = set()
    pages, attempts = [], 0

    while queue and attempts < max_pages:
        if time.monotonic() - started > max_seconds:
            log("time budget reached - stopping")
            break
        url = queue.popleft()
        if not robots.allowed(url):
            reason = "disallowed by robots.txt" if robots.parser else robots.note
            log(f"skipped ({reason}): {url}")
            page = _blank_page(url)
            page.update(status="skipped", error=reason)
            pages.append(page)
            continue
        attempts += 1
        if attempts > 1:
            time.sleep(delay)
        log(f"[{attempts}/{max_pages}] GET {url}")
        page = _blank_page(url)
        try:
            resp, final_url = fetch(session, url, site, timeout, validator)
            page["http_status"] = resp.status_code
            fkey = url_key(final_url)
            if fkey in visited_final:
                resp.close()
                attempts -= 1
                log("  duplicate of an already-crawled page - skipped")
                continue
            visited_final.add(fkey)
            if final_url != url:
                page["final_url"] = final_url
            if resp.status_code >= 400:
                resp.close()
                raise FetchError(f"HTTP {resp.status_code}")
            fields, internal = extract(read_body(resp), final_url, site)
            page.update(fields)
            page.update(status="ok", error=None)
            log(f"  ok: {fields['title']!r}, {fields['word_count']} words, "
                f"{fields['internal_link_count']} internal links")
            for link in internal:
                key = url_key(link)
                if key not in queued and len(queued) < max_pages * 20:
                    queued.add(key)
                    queue.append(link)
        except (BlockedURL, FetchError) as exc:
            page["error"] = str(exc)
            log(f"  failed: {exc}")
        pages.append(page)

    ok = [p for p in pages if p["status"] == "ok"]
    failed = [p for p in pages if p["status"] == "error"]
    skipped = [p for p in pages if p["status"] == "skipped"]
    error = None
    if not ok:
        status = "failed"
        first = (failed or skipped or [{}])[0]
        error = first.get("error") or "no page could be crawled"
    elif failed:
        status = "partial"
    else:
        status = "success"
    return {
        "status": status,
        "start_url": start_url,
        "domain": site,
        "max_pages": max_pages,
        "pages_crawled": len(ok),
        "pages_failed": len(failed),
        "pages_skipped": len(skipped),
        "robots": robots.note,
        "duration_seconds": round(time.monotonic() - started, 2),
        "error": error,
        "pages": pages,
    }


# --------------------------------------------------------------------------- CLI
def emit(doc: dict) -> None:
    sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="crawler_cli.py",
        description="Safe same-domain website crawler. Prints one JSON document to stdout.",
    )
    ap.add_argument("--url", required=True, help="http(s) URL to start from")
    ap.add_argument("--max-pages", type=int, default=3, help=f"pages to crawl (1-{HARD_MAX_PAGES}, default 3)")
    ap.add_argument("--output", choices=["json"], default="json", help="output format")
    ap.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds")
    ap.add_argument("--max-seconds", type=float, default=90.0, help="total time budget in seconds")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    max_pages = max(1, min(args.max_pages, HARD_MAX_PAGES))
    if max_pages != args.max_pages:
        log(f"--max-pages clamped to {max_pages}")
    try:
        result = crawl(args.url, max_pages, timeout=args.timeout, max_seconds=args.max_seconds)
    except BlockedURL as exc:
        log(f"refused: {exc}")
        emit({"status": "failed", "start_url": args.url, "error": f"URL refused: {exc}", "pages": []})
        return EXIT_INVALID
    except KeyboardInterrupt:
        emit({"status": "failed", "start_url": args.url, "error": "interrupted", "pages": []})
        return EXIT_FAILED
    except Exception as exc:  # last-resort guard: always honour the JSON contract
        log(f"unexpected error: {type(exc).__name__}: {exc}")
        emit({"status": "failed", "start_url": args.url, "error": f"unexpected error: {type(exc).__name__}", "pages": []})
        return EXIT_FAILED
    emit(result)
    return {"success": EXIT_OK, "partial": EXIT_PARTIAL}.get(result["status"], EXIT_FAILED)


if __name__ == "__main__":
    sys.exit(main())
