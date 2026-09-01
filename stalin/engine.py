"""Fetcher engines + the fetch classifier.

Rule zero of healing: never diagnose drift on a page you didn't really get.
Every fetch is classified BEFORE any drift logic sees it. A blocked page can
never reach the healer.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import urlparse

import httpx

from .utils import ema, parse_duration, parse_rate

USER_AGENT = "stalin/0.1 (+https://github.com/DreadpiratePickles/stalin)"

_CHALLENGE_MARKERS = (
    "cf-chl", "cf_chl", "captcha", "just a moment", "attention required",
    "access denied", "are you a robot", "enable javascript and cookies",
    "ddos protection", "checking your browser",
)
_BLOCK_URL_HINTS = ("/login", "/signin", "/challenge", "/denied", "/captcha")


class FetchBlocked(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class RobotsDisallowed(Exception):
    pass


@dataclass
class FetchResult:
    status: int
    url_final: str
    headers: dict
    html: str
    elapsed: float
    not_modified: bool = False
    engine: str = "httpx"


class Engine(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...


# --- politeness ----------------------------------------------------------

class RateLimiter:
    """Per-host min-interval limiter with +/-30% jitter."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, host: str) -> None:
        async with self._lock:
            last = self._last.get(host, 0.0)
            interval = self.min_interval * random.uniform(0.7, 1.3)
            delay = max(0.0, last + interval - time.monotonic())
            self._last[host] = time.monotonic() + delay
        if delay > 0:
            await asyncio.sleep(delay)


class RobotsCache:
    def __init__(self, respect: bool):
        self.respect = respect
        self._parsers: dict[str, urllib.robotparser.RobotFileParser] = {}

    async def allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        if not self.respect:
            return True
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._parsers:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = await client.get(f"{origin}/robots.txt",
                                     headers={"User-Agent": USER_AGENT}, timeout=10)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp.parse([])          # no robots -> allowed
            except httpx.HTTPError:
                rp.parse([])
            self._parsers[origin] = rp
        return self._parsers[origin].can_fetch(USER_AGENT, url)


class HttpCache:
    """ETag / Last-Modified store so unchanged pages cost a 304."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text()) if path.exists() else {}
        except json.JSONDecodeError:
            self.data = {}

    def headers_for(self, url: str) -> dict:
        e = self.data.get(url, {})
        h = {}
        if e.get("etag"):
            h["If-None-Match"] = e["etag"]
        if e.get("last_modified"):
            h["If-Modified-Since"] = e["last_modified"]
        return h

    def store(self, url: str, resp_headers: httpx.Headers) -> None:
        e = {}
        if resp_headers.get("etag"):
            e["etag"] = resp_headers["etag"]
        if resp_headers.get("last-modified"):
            e["last_modified"] = resp_headers["last-modified"]
        if e:
            self.data[url] = e

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1))


# --- the default engine --------------------------------------------------

@dataclass
class HttpxEngine:
    timeout: float = 20.0
    limiter: Optional[RateLimiter] = None
    robots: Optional[RobotsCache] = None
    cache: Optional[HttpCache] = None
    _client: Optional[httpx.AsyncClient] = field(default=None, repr=False)

    async def __aenter__(self) -> "HttpxEngine":
        self._client = httpx.AsyncClient(
            follow_redirects=True, timeout=self.timeout,
            headers={"User-Agent": USER_AGENT,
                     "Accept": "text/html,application/xhtml+xml"})
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    async def fetch(self, url: str, conditional: bool = True) -> FetchResult:
        assert self._client is not None, "use `async with HttpxEngine(...)`"
        host = urlparse(url).netloc
        if self.robots and not await self.robots.allowed(self._client, url):
            raise RobotsDisallowed(
                f"robots.txt disallows fetching {url} — set `robots: ignore` on the "
                f"source to override (it will warn on every run)")
        if self.limiter:
            await self.limiter.wait(host)
        headers = self.cache.headers_for(url) if (self.cache and conditional) else {}
        t0 = time.monotonic()
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                r = await self._client.get(url, headers=headers)
                break
            except httpx.HTTPError as e:
                last_err = e
                if attempt == 0:
                    await asyncio.sleep(1.5)
        else:
            raise FetchBlocked(f"network error: {last_err}")
        elapsed = time.monotonic() - t0
        if r.status_code == 304:
            return FetchResult(304, str(r.url), dict(r.headers), "", elapsed,
                               not_modified=True)
        if self.cache:
            self.cache.store(url, r.headers)
        return FetchResult(r.status_code, str(r.url), dict(r.headers), r.text, elapsed)


# --- the classifier ------------------------------------------------------

def classify(result: FetchResult, origin_host: str,
             content_len_ema: Optional[float]) -> Optional[str]:
    """Return a block reason, or None if the page is genuine.

    Runs before extraction, drift, everything. Healing against a challenge
    page teaches the tool to extract garbage; this is the guard.
    """
    if result.not_modified:
        return None
    if result.status in (401, 403, 407, 429, 503):
        return f"http {result.status}"
    if result.status >= 400:
        return f"http {result.status}"
    head = result.html[:4096].lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in head:
            return f"challenge page ({marker!r})"
    final = urlparse(result.url_final)
    if final.netloc and final.netloc != origin_host:
        return f"redirected off-host to {final.netloc}"
    if any(h in final.path.lower() for h in _BLOCK_URL_HINTS):
        return f"redirected to {final.path}"
    if content_len_ema and len(result.html) < 0.2 * content_len_ema:
        return (f"content collapsed ({len(result.html)}b vs "
                f"~{int(content_len_ema)}b baseline)")
    return None


def build_engine(project_config, source_spec) -> HttpxEngine:
    """Engine factory. One engine in v1; scrapling/playwright slot in here later."""
    d = project_config.defaults
    respect = (source_spec.robots or d.robots) == "respect"
    return HttpxEngine(
        timeout=parse_duration(d.timeout),
        limiter=RateLimiter(parse_rate(d.rate_limit)),
        robots=RobotsCache(respect),
    )
