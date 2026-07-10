"""
Email finder service (hybrid httpx + Playwright).

Given a business website URL, this service looks for a published contact
email on the homepage and up to 3 likely contact/about/team pages.

Fetch strategy, cheapest first:
  1. httpx — plain HTTP, fast and light. Handles most sites.
  2. Playwright (headless Chromium) — only when httpx is blocked (4xx/5xx)
     or loads pages but finds no email (the email may be rendered by JS).
  A dead domain (DNS/connection failure) is never escalated to Playwright:
  a browser cannot reach a server that doesn't answer at all.
"""

from typing import Optional

import html
import re
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

app = FastAPI(title="signal-to-send email finder")

# A realistic browser User-Agent, because many sites (and CDNs like
# Cloudflare) serve empty pages or 403s to obvious bot user agents.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

# httpx gets a hard 10-second cap so n8n isn't left waiting on slow sites.
TIMEOUT = httpx.Timeout(10.0)

# Playwright gets more room (20s) because launching a page and running its
# JavaScript is inherently slower than a raw HTTP fetch.
PW_TIMEOUT_MS = 20_000
# After the page loads, give its JS up to 5 extra seconds to settle
# (network idle). Some sites poll forever and never go idle — that's fine,
# we just take whatever is rendered by then.
PW_IDLE_MS = 5_000

# One regex covers both cases: a mailto: link like href="mailto:info@x.com"
# *contains* a plain email, so matching the email pattern alone finds both
# mailto links and emails written directly in the page text.
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,}")

# Crude <a href="...">text</a> matcher. Regex on HTML is normally a sin,
# but we only need hrefs + link text, and it avoids an HTML-parser
# dependency on a service meant to stay small.
LINK_RE = re.compile(
    r"<a\s[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)

# Pages most likely to publish a contact email.
CANDIDATE_KEYWORDS = ("contact", "about", "team")

# Regex email matching happily "finds" image filenames like
# logo@2x.png (they look like user@domain.ext), so we drop those.
JUNK_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

# Placeholder and platform-system addresses that appear in page source but
# are never a real contact (Wix/Sentry error reporting, GoDaddy templates,
# example.com documentation snippets).
JUNK_DOMAIN_PARTS = ("example.com", "example.org", "wixpress", "sentry", "godaddy")


class FindEmailRequest(BaseModel):
    website: str


def result(
    email: Optional[str] = None,
    email_status: str = "no_email_published",
    source_page: Optional[str] = None,
    method: Optional[str] = None,
    error: Optional[str] = None,
) -> dict:
    """Build the response JSON. The core fields are always present (null
    when unknown) so n8n field mappings never break; `error` only appears
    when there is a real diagnostic to report."""
    body = {
        "email": email,
        "email_status": email_status,
        "source_page": source_page,
        "method": method,
    }
    if error:
        body["error"] = error
    return body


def normalize_url(raw: str) -> str:
    """Add https:// if the caller sent a bare domain like 'example.com'."""
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def site_domain(url: str) -> str:
    """The site's hostname without 'www.', used for same-domain checks."""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_junk(email: str) -> bool:
    """Filter matches that fit the email pattern but aren't real contacts."""
    lower = email.lower()
    if lower.endswith(JUNK_SUFFIXES):
        return True
    domain = lower.rsplit("@", 1)[-1]
    return any(part in domain for part in JUNK_DOMAIN_PARTS)


def extract_emails(page_html: str) -> list[str]:
    """All plausible emails in the page, in order of appearance, deduped."""
    # Unescape first so emails written as e.g. info&#64;site.com still match.
    text = html.unescape(page_html)
    seen = []
    for match in EMAIL_RE.findall(text):
        email = match.lower()
        if email not in seen and not is_junk(email):
            seen.append(email)
    return seen


def pick_best(emails: list[str], domain: str) -> Optional[str]:
    """Prefer an email on the site's own domain — it's almost certainly the
    business's real address rather than a partner, widget, or agency email."""
    for email in emails:
        email_domain = email.rsplit("@", 1)[-1]
        if email_domain == domain or email_domain.endswith("." + domain):
            return email
    return emails[0] if emails else None


def find_candidate_links(page_html: str, base_url: str, domain: str) -> list[str]:
    """Up to 3 same-domain links that look like contact/about/team pages."""
    candidates = []
    for href, link_text in LINK_RE.findall(page_html):
        haystack = (href + " " + link_text).lower()
        if not any(kw in haystack for kw in CANDIDATE_KEYWORDS):
            continue
        # mailto:/tel:/js links aren't pages we can fetch.
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(base_url, href)
        # Stay on the same site — we don't want to crawl the wider web.
        if site_domain(full) != domain:
            continue
        if full not in candidates and full != base_url:
            candidates.append(full)
        if len(candidates) == 3:
            break
    return candidates


async def load_robots(client: httpx.AsyncClient, homepage: str) -> Optional[RobotFileParser]:
    """Fetch and parse robots.txt ourselves (RobotFileParser.read() would use
    urllib with no timeout, which could hang the whole request).
    Returns None if robots.txt is missing/unreachable — the web convention
    is that no robots.txt means crawling is allowed."""
    parsed = urlparse(homepage)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        resp = await client.get(robots_url)
        if resp.status_code != 200:
            return None
        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp
    except httpx.HTTPError:
        return None


def playwright_search(
    homepage: str, domain: str, robots: Optional[RobotFileParser]
) -> dict:
    """Second-chance search with a real headless browser, for sites that
    block plain HTTP clients or only render their email with JavaScript.

    This uses Playwright's *sync* API and runs in a worker thread, because
    driving Playwright's async API inside uvicorn's event loop is fragile
    (especially on Windows, where browser subprocess handling differs).

    Returns: {"email", "source_page", "loaded", "error"} — `loaded` tells the
    caller whether we ever got real page content (that's what separates
    "no email exists" from "the site blocked us")."""
    out = {"email": None, "source_page": None, "loaded": False, "error": None}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        # Service still works without Playwright — httpx results just
        # can't be double-checked by a browser.
        out["error"] = "playwright not installed"
        return out

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_context(user_agent=USER_AGENT).new_page()

                def render(url: str) -> Optional[str]:
                    """Load a URL and return its rendered HTML, or None if
                    the server answered with an error page (4xx/5xx)."""
                    resp = page.goto(url, timeout=PW_TIMEOUT_MS, wait_until="load")
                    try:
                        page.wait_for_load_state("networkidle", timeout=PW_IDLE_MS)
                    except Exception:
                        pass  # never going idle just means take what we have
                    if resp is not None and resp.status >= 400:
                        return None
                    return page.content()

                page_html = render(homepage)
                if page_html is None:
                    return out  # loaded stays False → caller reports "blocked"
                out["loaded"] = True

                emails = extract_emails(page_html)
                if emails:
                    out["email"] = pick_best(emails, domain)
                    out["source_page"] = page.url
                    return out

                for link in find_candidate_links(page_html, page.url, domain):
                    # robots.txt applies no matter which client fetches.
                    if robots and not robots.can_fetch(USER_AGENT, link):
                        continue
                    try:
                        sub_html = render(link)
                    except Exception:
                        continue  # one broken subpage shouldn't end the search
                    if sub_html is None:
                        continue
                    emails = extract_emails(sub_html)
                    if emails:
                        out["email"] = pick_best(emails, domain)
                        out["source_page"] = page.url
                        return out
            finally:
                browser.close()
    except Exception as exc:  # timeouts, crashed browser, missing chromium…
        out["error"] = f"playwright failed: {exc}"
    return out


@app.get("/")
async def health():
    """Health check so Render keeps the service marked healthy and n8n can
    ping it (also useful to keep the free-tier instance awake)."""
    return {"status": "alive"}


@app.post("/find-email")
async def find_email(req: FindEmailRequest) -> dict:
    homepage = normalize_url(req.website)
    domain = site_domain(homepage)
    if not domain:
        return result(email_status="site_unreachable", error="Invalid website URL")

    # Tracks whether httpx got real page content — decides later whether a
    # Playwright failure means "blocked" or just "no email on a working site".
    httpx_loaded = False
    httpx_error = None
    # Tracks whether the server ever answered with an HTTP response at all
    # (even a 403 counts). "Blocked" is only a fair label when the site is
    # provably alive and refusing us; a server that never responds is
    # unreachable, not blocking.
    server_responded = False

    async with httpx.AsyncClient(
        headers=HEADERS, timeout=TIMEOUT, follow_redirects=True
    ) as client:
        # Check robots.txt before touching the site — polite crawling. If the
        # site opts out we stop entirely: no Playwright end-run around it.
        robots = await load_robots(client, homepage)
        if robots and not robots.can_fetch(USER_AGENT, homepage):
            return result(email_status="robots_disallowed")

        # --- Phase 1: httpx (cheap, covers most sites) ---
        try:
            resp = await client.get(homepage)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # DNS failure / nothing listening: the domain is dead. A browser
            # can't reach it either, so escalating to Playwright is pointless.
            # (For lead scoring, a dead website is itself a distress signal.)
            return result(
                email_status="site_unreachable",
                error=f"Could not connect: {exc}",
            )
        except httpx.HTTPError as exc:
            # Connected but the transfer failed (slow server, protocol quirk).
            # The site may still be alive, so a real browser is worth a try —
            # but no HTTP response ever arrived, so if Playwright also fails
            # this ends as "site_unreachable", not "blocked".
            httpx_error = f"httpx failed: {exc}"
        else:
            server_responded = True
            if resp.status_code >= 400:
                # Server answered but refused us (403 anti-bot, 503, …).
                # A real browser often gets through where plain HTTP doesn't.
                httpx_error = f"HTTP {resp.status_code}"
            else:
                httpx_loaded = True
                page_html = resp.text
                emails = extract_emails(page_html)
                if emails:
                    return result(
                        email=pick_best(emails, domain),
                        email_status="published_on_site",
                        source_page=str(resp.url),
                        method="httpx",
                    )
                # No email on the homepage — try likely contact pages.
                # Links resolve against the *final* URL after redirects so
                # relative hrefs work when http://x.com became https://www.x.com.
                for link in find_candidate_links(page_html, str(resp.url), domain):
                    if robots and not robots.can_fetch(USER_AGENT, link):
                        continue
                    try:
                        sub = await client.get(link)
                        sub.raise_for_status()
                    except httpx.HTTPError:
                        continue
                    emails = extract_emails(sub.text)
                    if emails:
                        return result(
                            email=pick_best(emails, domain),
                            email_status="published_on_site",
                            source_page=str(sub.url),
                            method="httpx",
                        )

    # --- Phase 2: Playwright (httpx was refused, or found no email) ---
    # Runs in a thread so the browser work doesn't block the event loop.
    pw = await run_in_threadpool(playwright_search, homepage, domain, robots)

    if pw["email"]:
        return result(
            email=pw["email"],
            email_status="published_on_site",
            source_page=pw["source_page"],
            method="playwright",
        )
    if pw["loaded"]:
        # The browser saw the fully rendered site and there was still no
        # email — we can now confidently say none is published.
        return result(email_status="no_email_published", method="playwright")
    if httpx_loaded:
        # httpx read the site fine; Playwright just couldn't run (not
        # installed, crashed…). Trust the httpx result: no email published.
        return result(
            email_status="no_email_published", method="httpx", error=pw["error"]
        )
    if server_responded:
        # Server answered (403 anti-bot, 503, …) but neither plain HTTP nor a
        # real browser ever got a normal page out of it — anti-bot blocking.
        return result(email_status="blocked", error=pw["error"] or httpx_error)
    # We never got an HTTP response out of the server and the browser
    # couldn't load it either: the site is unreachable, not blocking us.
    return result(email_status="site_unreachable", error=pw["error"] or httpx_error)
