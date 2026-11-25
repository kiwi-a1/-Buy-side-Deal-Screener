"""Crawl a company website within limits: sitemap + links, robots.txt, relevance ranking, pagination caps,
broken-link reporting. Only public addresses are fetched (also after redirects)."""
from __future__ import annotations

import hashlib
import heapq
import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Optional
from urllib.parse import urldefrag, urljoin, urlparse, urlencode, parse_qsl
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "DealScreeningBot/2.0 (local investment research tool; respects robots.txt)"
MAX_BYTES = 2_000_000

# Path keywords -> relevance. Higher is fetched first and preferred for analysis.
PRIORITY = [
    (6, r"about|team|company|leadership|founder|people|who-we-are|management"),
    (5, r"product|platform|feature|solution|how-it-works|technology|services"),
    (5, r"pricing|plans"),
    (5, r"customer|case-stud|testimonial|success|clients"),
    (4, r"invest|funding|press|news|media|newsroom|announcement"),
    (3, r"career|jobs|hiring"),
    (2, r"partner|integration|security|compliance"),
    (1, r"blog|resources|docs|faq|contact"),
]
SKIP_PATH = re.compile(
    r"(login|log-in|signin|sign-in|signup|sign-up|register|cart|checkout|account|password|privacy|terms|cookie|legal|"
    r"gdpr|imprint|impressum|wp-admin|wp-json|feed|/tag/|/tags/|/category/|/author/|/search|cdn-cgi|unsubscribe)", re.I)
SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|svg|webp|ico|css|js|mjs|json|zip|gz|mp4|mp3|mov|avi|woff2?|ttf|eot|xml|rss|dmg|exe)$", re.I)
PAGINATION = re.compile(r"([?&](page|p|paged)=\d+|/page/\d+)", re.I)
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|ref|mc_|hsCta|_hs)", re.I)


class CrawlBlocked(Exception):
    pass


@dataclass
class Page:
    url: str
    title: str
    text: str
    priority: int
    depth: int


@dataclass
class Fetched:
    status_code: int
    url: str
    content_type: str
    text: str


@dataclass
class CrawlResult:
    start_url: str
    final_url: str = ""
    pages: list[Page] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)          # broken links / failures
    skipped: dict[str, int] = field(default_factory=dict)      # reason -> count
    sitemap_urls: int = 0
    reachable: bool = False
    stopped_reason: str = ""
    not_visited: int = 0                                       # relevant links left when a limit was hit

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._a: Optional[str] = None
        self._text: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._a, self._text = a["href"], []
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            self.links.append((self._a, " ".join(self._text).strip()))
            self._a = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._a is not None:
            self._text.append(data)
        if self._in_title:
            self.title += data


def is_public_host(host: str, resolve: Callable = socket.getaddrinfo) -> bool:
    try:
        infos = resolve(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def site_key(host: str) -> str:
    return (host or "").lower().removeprefix("www.")


def normalize(url: str) -> str:
    url, _ = urldefrag(url)
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not TRACKING_PARAMS.match(k)])
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return f"{p.scheme.lower()}://{(p.hostname or '').lower()}{(':' + str(p.port)) if p.port else ''}{path}{('?' + query) if query else ''}"


CONTENT_SECTION = re.compile(r"^/(blog|articles?|insights|resources|docs|documentation|help|support|kb|knowledge-base|"
                             r"academy|learn|guides?|glossary|podcasts?|webinars?|events|templates|community)(/|$)")
NEWS_SECTION = re.compile(r"^/(news|press|newsroom|media|press-releases)(/|$)")
# Other-language copies of the same page (deliberately excludes codes like "ai" that are also product paths).
LOCALE = re.compile(r"^/(de|es|fr|it|pl|pt|nl|sv|da|fi|no|nb|cs|sk|hu|ro|bg|hr|sl|sr|uk|ru|tr|el|ja|ko|zh|ar|he|id|th|"
                    r"vi|lt|lv|et)(-[a-z]{2})?(/|$)")


def priority_of(url: str, anchor: str = "") -> int:
    """Relevance from the top of the path only: /about is key, /blog/team-management is a blog post."""
    path = urlparse(url).path.lower()
    if NEWS_SECTION.match(path):
        return 4  # press pages often carry funding and customer announcements
    if CONTENT_SECTION.match(path):
        return 1
    hay = ([seg for seg in path.split("/") if seg] or [""])[0] + " " + anchor.lower()
    return max([score for score, pat in PRIORITY if re.search(pat, hay)], default=0)


def extract_text(html: str, url: str) -> str:
    import trafilatura
    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True, favor_recall=True) or ""
    if not text:  # fallback: visible text without scripts/styles
        text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+\n", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def crawl(start_url: str, max_pages: int = 20, max_depth: int = 3, time_budget: float = 75.0, per_pattern: int = 3, per_section: int = 3,
          client: Optional[httpx.Client] = None, check_host: Callable[[str], bool] = is_public_host,
          on_progress: Callable[[str], None] = lambda m: None) -> CrawlResult:
    res = CrawlResult(start_url=start_url)
    t0 = time.monotonic()

    def guard(request: httpx.Request) -> None:  # runs for every request, including each redirect hop
        if not check_host(request.url.host):
            raise CrawlBlocked(f"{request.url.host}: address not found, or not a public internet address")

    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(10.0, connect=6.0), max_redirects=5,
                                    headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    client.event_hooks["request"] = [guard]

    def get(url: str) -> Optional[Fetched]:
        try:
            with client.stream("GET", url) as r:
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:  # stop downloading, don't hold huge pages in memory
                        res.errors.append({"url": url, "error": "page larger than 2 MB"})
                        return None
                return Fetched(r.status_code, str(r.url), r.headers.get("content-type", ""),
                               bytes(body).decode(r.encoding or "utf-8", errors="replace"))
        except CrawlBlocked as e:
            res.errors.append({"url": url, "error": str(e)})
        except httpx.TimeoutException:
            res.errors.append({"url": url, "error": "timed out"})
        except httpx.HTTPError as e:
            res.errors.append({"url": url, "error": type(e).__name__})
        return None

    try:
        home = get(start_url)
        if home is None or home.status_code >= 400:
            if home is not None:
                res.errors.append({"url": start_url, "error": f"HTTP {home.status_code}"})
            res.stopped_reason = "homepage unreachable"
            return res
        res.reachable = True
        res.final_url = normalize(home.url)
        base = urlparse(res.final_url)
        key = site_key(base.hostname)

        robots = RobotFileParser()
        r = get(f"{base.scheme}://{base.netloc}/robots.txt")
        robots.parse(r.text.splitlines() if r is not None and r.status_code == 200 else [])
        sitemaps = list(robots.site_maps() or []) or [f"{base.scheme}://{base.netloc}/sitemap.xml"]

        seen: set[str] = set()
        hashes: set[str] = set()
        patterns: dict[str, int] = {}
        sections: dict[str, int] = {}
        queue: list[tuple[int, int, str, str]] = []  # (-priority, depth, url, anchor)

        def enqueue(url: str, depth: int, anchor: str = "") -> None:
            url = normalize(url)
            p = urlparse(url)
            if p.scheme not in ("http", "https") or site_key(p.hostname) != key:
                return
            if url in seen:
                return
            seen.add(url)
            if SKIP_EXT.search(p.path) or SKIP_PATH.search(p.path):
                res.skip("not useful (asset, login, legal...)")
                return
            if LOCALE.match(p.path.lower()) and not LOCALE.match(urlparse(res.final_url).path.lower()):
                res.skip("other-language copy")
                return
            if PAGINATION.search(url):
                pattern = PAGINATION.sub("", url)
                patterns[pattern] = patterns.get(pattern, 0) + 1
                if patterns[pattern] > per_pattern:
                    res.skip("pagination limit")
                    return
            if not robots.can_fetch(USER_AGENT, url):
                res.skip("disallowed by robots.txt")
                return
            heapq.heappush(queue, (depth - priority_of(url, anchor), depth, url, anchor))  # relevant + shallow first

        seen.add(res.final_url)
        for sm in sitemaps[:2]:  # sitemap (or sitemap index -> first child sitemaps)
            s = get(sm)
            if s is None or s.status_code != 200:
                continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", s.text)[:300]
            if "<sitemapindex" in s.text:
                for child in locs[:3]:
                    c = get(child)
                    if c is not None and c.status_code == 200:
                        locs += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", c.text)[:300]
            res.sitemap_urls += len(locs)
            for loc in locs:
                enqueue(loc, 1)

        def handle(resp: Fetched, url: str, depth: int) -> None:
            if "html" not in resp.content_type:
                res.skip("not a web page")
                return
            parser = _Links()
            try:
                parser.feed(resp.text)
            except Exception:  # malformed HTML: keep what was parsed
                pass
            text = extract_text(resp.text, url)
            digest = hashlib.sha1(re.sub(r"\W+", "", text.lower()).encode()).hexdigest()
            if len(text) < 80:
                res.skip("almost no text")
            elif digest in hashes:
                res.skip("duplicate content")
            else:
                hashes.add(digest)
                res.pages.append(Page(url=url, title=re.sub(r"\s+", " ", parser.title).strip()[:160], text=text,
                                      priority=priority_of(url), depth=depth))
            if depth < max_depth:
                for href, anchor in parser.links:
                    if not href.startswith(("mailto:", "tel:", "javascript:")):
                        enqueue(urljoin(resp.url, href), depth + 1, anchor)

        handle(home, res.final_url, 0)
        while queue and len(res.pages) < max_pages:
            if time.monotonic() - t0 > time_budget:
                res.stopped_reason = f"time limit ({int(time_budget)}s)"
                break
            _, depth, url, _ = heapq.heappop(queue)
            section = ([seg for seg in urlparse(url).path.split("/") if seg] or ["/"])[0]
            if sections.get(section, 0) >= per_section:  # breadth: a few case studies, not twenty
                res.skip("section limit")
                continue
            sections[section] = sections.get(section, 0) + 1
            on_progress(f"Crawling website: {len(res.pages)} pages read...")
            resp = get(url)
            if resp is None:
                continue
            if resp.status_code >= 400:
                res.errors.append({"url": url, "error": f"HTTP {resp.status_code}"})
                continue
            handle(resp, url, depth)
        if not res.stopped_reason:
            res.stopped_reason = f"page limit ({max_pages})" if queue else "all reachable pages read"
        res.not_visited = len(queue)
        return res
    finally:
        if own:
            client.close()
