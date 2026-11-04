"""A small SOL/USD price service.

The firehose is SOL-denominated. USD market caps are a convenience, so this
service uses public spot endpoints with a short timeout and keeps the last good
value when a provider is down. Pin the price with ``--sol-usd`` when you need
reproducible numbers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .models import CoinBook

LOGGER = logging.getLogger("pumpfire.solprice")

_SOL_MINT = "So11111111111111111111111111111111111111112"


class SolPrice:
    """Fetch and cache the SOL/USD spot price."""

    def __init__(
        self,
        book: CoinBook,
        *,
        fixed: float | None = None,
        interval_s: float = 60.0,
        timeout: float = 6.0,
    ) -> None:
        self.book = book
        self.fixed = fixed if fixed and fixed > 0 else None
        self.interval_s = max(15.0, interval_s)
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None
        if self.fixed is not None:
            book.set_sol_usd(self.fixed, source="fixed", now=time.time())

    async def start(self) -> None:
        if self.fixed is not None:
            return
        self._client = httpx.AsyncClient(timeout=self.timeout)
        self._task = asyncio.create_task(self._run(), name="pumpfire-solprice")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                price, source = await self._fetch()
                if price > 0:
                    self.book.set_sol_usd(price, source=source)
                    self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)[:160]
                LOGGER.debug("sol price refresh failed: %s", exc)
            await asyncio.sleep(self.interval_s)

    async def _fetch(self) -> tuple[float, str]:
        assert self._client is not None
        for source, url, extractor in (
            ("coinbase", "https://api.coinbase.com/v2/prices/SOL-USD/spot", _coinbase),
            ("kraken", "https://api.kraken.com/0/public/Ticker?pair=SOLUSD", _kraken),
            (
                "coingecko",
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                _coingecko,
            ),
            (
                "jupiter",
                f"https://lite-api.jup.ag/price/v3?ids={_SOL_MINT}",
                _jupiter,
            ),
        ):
            try:
                response = await self._client.get(url)
                if response.status_code != 200:
                    continue
                price = extractor(response.json())
                if price and price > 0:
                    return float(price), source
            except Exception:
                continue
        return 0.0, ""

    def health(self) -> dict[str, Any]:
        return {
            "usd": round(self.book.sol_usd, 4),
            "source": self.book.sol_price_source,
            "age_s": (
                round(time.time() - self.book.sol_price_ts, 1) if self.book.sol_price_ts else None
            ),
            "fixed": self.fixed is not None,
            "last_error": self.last_error,
        }


def _coinbase(payload: dict[str, Any]) -> float | None:
    return _as_float((payload.get("data") or {}).get("amount"))


def _kraken(payload: dict[str, Any]) -> float | None:
    result = payload.get("result") or {}
    for value in result.values():
        if isinstance(value, dict) and "c" in value:
            return _as_float((value.get("c") or [None])[0])
    return None


def _coingecko(payload: dict[str, Any]) -> float | None:
    return _as_float((payload.get("solana") or {}).get("usd"))


def _jupiter(payload: dict[str, Any]) -> float | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        entry = data.get(_SOL_MINT)
        if isinstance(entry, dict):
            return _as_float(entry.get("usdPrice") or entry.get("price"))
        if isinstance(entry, (int, float)):
            return _as_float(entry)
    entry = payload.get(_SOL_MINT) if isinstance(payload, dict) else None
    if isinstance(entry, dict):
        return _as_float(entry.get("usdPrice") or entry.get("price"))
    return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
