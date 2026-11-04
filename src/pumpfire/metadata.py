"""Optional token metadata enrichment.

A CreateEvent carries a ``uri`` pointing at a JSON document (usually IPFS or
Arweave). Fetching that document gives the image, description, and socials that
make a coin list readable. It is intentionally optional and bounded: a small
worker pool, a short timeout, a cache, and a private-address guard.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from .models import CoinBook

LOGGER = logging.getLogger("pumpfire.metadata")

_IPFS_GATEWAY = "https://ipfs.io/ipfs/"
_AR_GATEWAY = "https://arweave.net/"


def normalize_uri(uri: str) -> str | None:
    """Turn ipfs:// and ar:// URIs into fetchable HTTPS URLs."""
    uri = (uri or "").strip()
    if not uri:
        return None
    if uri.startswith("ipfs://"):
        return _IPFS_GATEWAY + uri[len("ipfs://") :].lstrip("/")
    if uri.startswith("ar://"):
        return _AR_GATEWAY + uri[len("ar://") :].lstrip("/")
    if uri.startswith(("http://", "https://")):
        return uri
    return None


def safe_url(url: str) -> bool:
    """Reject obvious SSRF targets. This is a guard rail, not a full sandbox."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in {"localhost", "localhost.localdomain", "0.0.0.0"}:
        return False
    if host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _social_url(value: str | None, *, kind: str) -> str | None:
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    if kind == "twitter":
        return "https://x.com/" + value.lstrip("@")
    if kind == "telegram":
        return "https://t.me/" + value.lstrip("@")
    return None


def clean_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract only the fields the dashboard uses, with safe URLs."""
    image = raw.get("image") or raw.get("image_uri")
    image = normalize_uri(image) if isinstance(image, str) else None
    website = raw.get("website") or raw.get("external_url")
    if isinstance(website, str) and website.startswith(("http://", "https://")):
        website_url = website
    else:
        website_url = None
    description = raw.get("description")
    description = description.strip()[:600] if isinstance(description, str) else ""
    return {
        "image": image if image and safe_url(image) else None,
        "description": description,
        "twitter": _social_url(raw.get("twitter"), kind="twitter"),
        "telegram": _social_url(raw.get("telegram"), kind="telegram"),
        "website": website_url,
        "created_on": raw.get("createdOn") or raw.get("created_on"),
    }


class MetadataFetcher:
    """A bounded background fetcher for token metadata documents."""

    def __init__(
        self,
        book: CoinBook,
        *,
        concurrency: int = 4,
        queue_size: int = 5_000,
        timeout: float = 6.0,
        enabled: bool = True,
    ) -> None:
        self.book = book
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.enabled = enabled
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=queue_size)
        self.cache: dict[str, dict[str, Any]] = {}
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if not self.enabled:
            return
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "pumpfire/0.1 (+https://github.com/0xJ1nn/pumpfun-firehose)"},
        )
        for index in range(self.concurrency):
            self._tasks.append(
                asyncio.create_task(self._worker(), name=f"pumpfire-metadata-{index}")
            )

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def enqueue(self, mint: str, uri: str) -> None:
        if not self.enabled:
            self.book.metadata_stats["skipped"] += 1
            return
        cached = self.cache.get(uri)
        if cached is not None:
            self.book.set_metadata(mint, cached)
            self.book.metadata_stats["ok"] += 1
            return
        try:
            self.queue.put_nowait((mint, uri))
            self.book.metadata_stats["queued"] += 1
        except asyncio.QueueFull:
            self.book.metadata_stats["skipped"] += 1

    async def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                mint, uri = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                data = await self._fetch(uri)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.book.metadata_stats["failed"] += 1
                LOGGER.debug("metadata fetch failed for %s: %s", mint[:8], exc)
                continue
            if data:
                self.cache[uri] = data
                self.book.set_metadata(mint, data)
                self.book.metadata_stats["ok"] += 1
            else:
                self.book.metadata_stats["failed"] += 1

    async def _fetch(self, uri: str) -> dict[str, Any] | None:
        url = normalize_uri(uri)
        if url is None or not safe_url(url):
            return None
        if self._client is None:
            return None
        response = await self._client.get(url)
        if response.status_code != 200:
            return None
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower() and not response.text.lstrip().startswith("{"):
            return None
        try:
            raw = response.json()
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        return clean_metadata(raw)

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "queue_depth": self.queue.qsize(),
            "cache_size": len(self.cache),
            "workers": len(self._tasks),
            **self.book.metadata_stats,
        }
