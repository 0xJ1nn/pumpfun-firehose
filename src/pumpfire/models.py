"""Event models and the in-memory coin book.

The book is deliberately a read model, not a strategy. It aggregates the
firehose into the numbers a dashboard, screener, or bot needs: price, market
cap, volume, flow, unique traders, curve progress, and graduation state.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from .constants import (
    INITIAL_REAL_TOKEN,
    INITIAL_VIRTUAL_SOL,
    INITIAL_VIRTUAL_TOKEN,
    TOTAL_SUPPLY,
    UNIQUE_WALLET_CAP,
)


class RateMeter:
    """A small rolling events-per-second meter."""

    __slots__ = ("window_s", "_events")

    def __init__(self, window_s: float = 10.0) -> None:
        self.window_s = window_s
        self._events: deque[tuple[float, int]] = deque()

    def add(self, count: int = 1, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._events.append((now, count))
        self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def rate(self, *, now: float | None = None) -> float:
        now = time.time() if now is None else now
        self._trim(now)
        if not self._events:
            return 0.0
        total = sum(count for _, count in self._events)
        span = max(1.0, min(self.window_s, now - self._events[0][0]))
        return round(total / span, 3)


@dataclass(slots=True)
class TradeEvent:
    """One decoded pump.fun trade leg."""

    signature: str
    mint: str
    user: str
    is_buy: bool
    sol: float
    tokens: float
    tokens_raw: int
    price_sol: float | None
    timestamp: int
    slot: int | None = None
    seen_ts: float = 0.0
    virtual_sol: float | None = None
    virtual_token: float | None = None
    real_sol: float | None = None
    real_token: float | None = None
    fee_sol: float | None = None
    creator: str | None = None
    creator_fee_sol: float | None = None
    kind: str = "trade"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "signature": self.signature,
            "mint": self.mint,
            "user": self.user,
            "is_buy": self.is_buy,
            "side": "buy" if self.is_buy else "sell",
            "sol": round(self.sol, 9),
            "tokens": round(self.tokens, 6),
            "tokens_raw": self.tokens_raw,
            "price_sol": _round(self.price_sol, 15),
            "timestamp": self.timestamp,
            "slot": self.slot,
            "seen_ts": round(self.seen_ts, 3),
            "virtual_sol": _round(self.virtual_sol, 9),
            "virtual_token": _round(self.virtual_token, 6),
            "real_sol": _round(self.real_sol, 9),
            "real_token": _round(self.real_token, 6),
            "fee_sol": _round(self.fee_sol, 9),
            "creator": self.creator,
            "creator_fee_sol": _round(self.creator_fee_sol, 9),
        }


@dataclass(slots=True)
class CreateEvent:
    """A new token launched on the pump.fun bonding curve."""

    signature: str
    mint: str
    name: str
    symbol: str
    uri: str
    creator: str
    bonding_curve: str
    timestamp: int
    slot: int | None = None
    seen_ts: float = 0.0
    virtual_sol: float | None = None
    virtual_token: float | None = None
    real_token: float | None = None
    total_supply: float | None = None
    kind: str = "create"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "signature": self.signature,
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "uri": self.uri,
            "creator": self.creator,
            "bonding_curve": self.bonding_curve,
            "timestamp": self.timestamp,
            "slot": self.slot,
            "seen_ts": round(self.seen_ts, 3),
            "virtual_sol": _round(self.virtual_sol, 9),
            "virtual_token": _round(self.virtual_token, 6),
            "real_token": _round(self.real_token, 6),
            "total_supply": _round(self.total_supply, 6),
        }


@dataclass(slots=True)
class CompleteEvent:
    """A bonding curve reached 100% and graduated."""

    signature: str
    mint: str
    user: str
    bonding_curve: str
    timestamp: int
    slot: int | None = None
    seen_ts: float = 0.0
    kind: str = "complete"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "signature": self.signature,
            "mint": self.mint,
            "user": self.user,
            "bonding_curve": self.bonding_curve,
            "timestamp": self.timestamp,
            "slot": self.slot,
            "seen_ts": round(self.seen_ts, 3),
        }


@dataclass(slots=True)
class DecodedBatch:
    """All pump.fun events found in one transaction notification."""

    signature: str
    slot: int | None
    trades: list[TradeEvent] = field(default_factory=list)
    creates: list[CreateEvent] = field(default_factory=list)
    completes: list[CompleteEvent] = field(default_factory=list)

    @property
    def events(self) -> list[TradeEvent | CreateEvent | CompleteEvent]:
        return [*self.creates, *self.trades, *self.completes]


@dataclass(slots=True)
class Coin:
    """Aggregated live state for one mint."""

    mint: str
    name: str = ""
    symbol: str = ""
    uri: str = ""
    creator: str = ""
    bonding_curve: str = ""
    created_ts: float = 0.0
    first_seen_ts: float = 0.0
    last_ts: float = 0.0
    first_price_sol: float = 0.0
    price_sol: float = 0.0
    market_cap_sol: float = 0.0
    virtual_sol: float = 0.0
    virtual_token: float = 0.0
    real_sol: float = 0.0
    real_token: float = 0.0
    total_supply: float = TOTAL_SUPPLY
    volume_sol: float = 0.0
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    buys: int = 0
    sells: int = 0
    trade_count: int = 0
    unique_buyers: set[str] = field(default_factory=set)
    unique_sellers: set[str] = field(default_factory=set)
    unique_capped: bool = False
    graduated: bool = False
    graduated_ts: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    snapshots: deque[tuple[float, float, float, int, int]] = field(
        default_factory=lambda: deque(maxlen=240)
    )
    recent: deque[TradeEvent] = field(default_factory=lambda: deque(maxlen=300))
    last_snapshot_ts: float = 0.0

    # ── derived ─────────────────────────────────────────────────────────
    def age_s(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        origin = self.created_ts or self.first_seen_ts or now
        return max(0.0, now - origin)

    def curve_progress_pct(self) -> float:
        if self.real_token > 0 and INITIAL_REAL_TOKEN > 0:
            pct = (1.0 - self.real_token / INITIAL_REAL_TOKEN) * 100.0
        elif self.virtual_token > 0:
            pct = ((INITIAL_VIRTUAL_TOKEN - self.virtual_token) / INITIAL_REAL_TOKEN) * 100.0
        else:
            pct = 0.0
        return round(min(100.0, max(0.0, pct)), 2)

    def snapshot(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if not self.snapshots or now - self.snapshots[-1][0] >= 5.0:
            self.snapshots.append(
                (now, self.volume_sol, self.price_sol, self.buys, self.sells)
            )
            self.last_snapshot_ts = now

    def _at_or_before(self, cutoff: float) -> tuple[float, float, float, int, int] | None:
        for snap in reversed(self.snapshots):
            if snap[0] <= cutoff:
                return snap
        return None

    def _baseline(self, seconds: float, now: float) -> tuple[float, float, int, int]:
        """Return (volume, price, buys, sells) at the start of a window."""
        base = self._at_or_before(now - seconds)
        if base is not None:
            return base[1], base[2], base[3], base[4]
        if self.age_s(now) <= seconds:
            return 0.0, self.first_price_sol, 0, 0
        # Older than the window but no snapshot inside it: no trade has moved
        # the price or volume since the last snapshot, so the window is flat.
        return self.volume_sol, self.price_sol, self.buys, self.sells

    def volume_window(self, seconds: float, now: float | None = None) -> float:
        now = time.time() if now is None else now
        base_volume, _, _, _ = self._baseline(seconds, now)
        return round(max(0.0, self.volume_sol - base_volume), 6)

    def change_window(self, seconds: float, now: float | None = None) -> float:
        now = time.time() if now is None else now
        if self.price_sol <= 0:
            return 0.0
        _, base_price, _, _ = self._baseline(seconds, now)
        if base_price <= 0:
            return 0.0
        return round((self.price_sol / base_price - 1.0) * 100.0, 4)

    def flow_window(self, seconds: float, now: float | None = None) -> tuple[int, int]:
        now = time.time() if now is None else now
        _, _, base_buys, base_sells = self._baseline(seconds, now)
        return max(0, self.buys - base_buys), max(0, self.sells - base_sells)

    # ── serialisation ───────────────────────────────────────────────────
    def to_dict(self, *, sol_usd: float = 0.0, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        price_usd = self.price_sol * sol_usd if sol_usd else 0.0
        mcap_usd = self.market_cap_sol * sol_usd if sol_usd else 0.0
        buys_1m, sells_1m = self.flow_window(60, now)
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "uri": self.uri,
            "creator": self.creator,
            "bonding_curve": self.bonding_curve,
            "created_ts": round(self.created_ts, 3),
            "first_seen_ts": round(self.first_seen_ts, 3),
            "last_ts": round(self.last_ts, 3),
            "age_s": round(self.age_s(now), 2),
            "price_sol": _round(self.price_sol, 15),
            "price_usd": _round(price_usd, 15),
            "market_cap_sol": _round(self.market_cap_sol, 6),
            "market_cap_usd": _round(mcap_usd, 2),
            "virtual_sol": _round(self.virtual_sol, 9),
            "virtual_token": _round(self.virtual_token, 6),
            "real_sol": _round(self.real_sol, 9),
            "real_token": _round(self.real_token, 6),
            "total_supply": _round(self.total_supply, 6),
            "curve_progress_pct": self.curve_progress_pct(),
            "volume_sol": _round(self.volume_sol, 6),
            "buy_sol": _round(self.buy_sol, 6),
            "sell_sol": _round(self.sell_sol, 6),
            "volume_1m_sol": self.volume_window(60, now),
            "volume_5m_sol": self.volume_window(300, now),
            "buys": self.buys,
            "sells": self.sells,
            "buys_1m": buys_1m,
            "sells_1m": sells_1m,
            "trade_count": self.trade_count,
            "unique_buyers": len(self.unique_buyers),
            "unique_sellers": len(self.unique_sellers),
            "unique_capped": self.unique_capped,
            "change_1m_pct": self.change_window(60, now),
            "change_5m_pct": self.change_window(300, now),
            "graduated": self.graduated,
            "graduated_ts": round(self.graduated_ts, 3),
            "metadata": self.metadata,
            "last_trade": self.recent[-1].as_dict() if self.recent else None,
        }

    def to_summary_dict(self, *, sol_usd: float = 0.0, now: float | None = None) -> dict[str, Any]:
        """A smaller payload for the live table."""
        data = self.to_dict(sol_usd=sol_usd, now=now)
        data.pop("last_trade", None)
        return data


@dataclass(slots=True)
class BookStats:
    started_ts: float = field(default_factory=time.time)
    messages: int = 0
    notifications: int = 0
    decoded: int = 0
    trades: int = 0
    creates: int = 0
    completes: int = 0
    errors: int = 0
    reconnects: int = 0
    dropped: int = 0
    duplicate_signatures: int = 0
    volume_sol: float = 0.0
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    last_message_ts: float = 0.0
    last_trade_ts: float = 0.0
    endpoint: str = ""
    message_rate: RateMeter = field(default_factory=RateMeter)
    trade_rate: RateMeter = field(default_factory=RateMeter)

    def as_dict(self, *, coins: int, graduated: int, sol_usd: float) -> dict[str, Any]:
        now = time.time()
        return {
            "uptime_s": round(now - self.started_ts, 1),
            "messages": self.messages,
            "notifications": self.notifications,
            "decoded": self.decoded,
            "trades": self.trades,
            "creates": self.creates,
            "completes": self.completes,
            "errors": self.errors,
            "reconnects": self.reconnects,
            "dropped": self.dropped,
            "duplicate_signatures": self.duplicate_signatures,
            "volume_sol": round(self.volume_sol, 6),
            "buy_sol": round(self.buy_sol, 6),
            "sell_sol": round(self.sell_sol, 6),
            "last_message_age_s": (
                round(now - self.last_message_ts, 1) if self.last_message_ts else None
            ),
            "last_trade_age_s": round(now - self.last_trade_ts, 1) if self.last_trade_ts else None,
            "messages_per_sec": self.message_rate.rate(now=now),
            "trades_per_sec": self.trade_rate.rate(now=now),
            "coins": coins,
            "graduated": graduated,
            "sol_usd": round(sol_usd, 4),
            "endpoint": self.endpoint,
        }


class CoinBook:
    """Bounded, thread-free in-memory aggregation of the firehose."""

    def __init__(
        self,
        *,
        max_coins: int = 10_000,
        recent_trades: int = 2_000,
        recent_creates: int = 500,
        recent_completes: int = 200,
        sol_usd: float = 0.0,
    ) -> None:
        self.max_coins = max_coins
        self.coins: OrderedDict[str, Coin] = OrderedDict()
        self.trades: deque[TradeEvent] = deque(maxlen=recent_trades)
        self.creates: deque[CreateEvent] = deque(maxlen=recent_creates)
        self.completes: deque[CompleteEvent] = deque(maxlen=recent_completes)
        self.stats = BookStats()
        self.sol_usd = sol_usd
        self.sol_price_source = "fixed" if sol_usd else ""
        self.sol_price_ts = 0.0
        self.metadata_stats: dict[str, Any] = {
            "queued": 0,
            "ok": 0,
            "failed": 0,
            "skipped": 0,
        }

    # ── ingest ──────────────────────────────────────────────────────────
    def get_or_create(self, mint: str, now: float | None = None) -> Coin:
        now = time.time() if now is None else now
        coin = self.coins.get(mint)
        if coin is None:
            if len(self.coins) >= self.max_coins:
                self.coins.popitem(last=False)
            coin = Coin(mint=mint, first_seen_ts=now, last_ts=now)
            self.coins[mint] = coin
        else:
            self.coins.move_to_end(mint)
        return coin

    def apply_trade(self, event: TradeEvent) -> Coin:
        now = event.seen_ts or time.time()
        coin = self.get_or_create(event.mint, now)
        has_reserves = (
            event.virtual_sol is not None
            and event.virtual_sol > 0
            and event.virtual_token is not None
            and event.virtual_token > 0
        )
        if event.price_sol and event.price_sol > 0:
            if coin.first_price_sol <= 0:
                coin.first_price_sol = event.price_sol
            coin.price_sol = event.price_sol
        if event.virtual_sol is not None and event.virtual_sol > 0:
            coin.virtual_sol = event.virtual_sol
        if event.virtual_token is not None and event.virtual_token > 0:
            coin.virtual_token = event.virtual_token
        if event.real_sol is not None and event.real_sol >= 0:
            coin.real_sol = event.real_sol
        if event.real_token is not None and event.real_token >= 0:
            coin.real_token = event.real_token
        if has_reserves and coin.virtual_sol > 0 and coin.virtual_token > 0:
            coin.price_sol = coin.virtual_sol / coin.virtual_token
            if coin.first_price_sol <= 0:
                coin.first_price_sol = coin.price_sol
        coin.market_cap_sol = coin.price_sol * coin.total_supply
        coin.last_ts = now
        coin.volume_sol += event.sol
        coin.trade_count += 1
        if event.is_buy:
            coin.buys += 1
            coin.buy_sol += event.sol
            if len(coin.unique_buyers) < UNIQUE_WALLET_CAP:
                coin.unique_buyers.add(event.user)
            else:
                coin.unique_capped = True
        else:
            coin.sells += 1
            coin.sell_sol += event.sol
            if len(coin.unique_sellers) < UNIQUE_WALLET_CAP:
                coin.unique_sellers.add(event.user)
            else:
                coin.unique_capped = True
        coin.recent.append(event)
        coin.snapshot(now)
        self.trades.append(event)
        self.stats.trades += 1
        self.stats.volume_sol += event.sol
        if event.is_buy:
            self.stats.buy_sol += event.sol
        else:
            self.stats.sell_sol += event.sol
        self.stats.last_trade_ts = now
        self.stats.trade_rate.add(now=now)
        return coin

    def apply_create(self, event: CreateEvent) -> Coin:
        now = event.seen_ts or time.time()
        coin = self.get_or_create(event.mint, now)
        if event.name:
            coin.name = event.name
        if event.symbol:
            coin.symbol = event.symbol
        if event.uri:
            coin.uri = event.uri
        if event.creator:
            coin.creator = event.creator
        if event.bonding_curve:
            coin.bonding_curve = event.bonding_curve
        coin.created_ts = float(event.timestamp or now)
        if event.total_supply and event.total_supply > 0:
            coin.total_supply = event.total_supply
        if event.virtual_sol and event.virtual_sol > 0:
            coin.virtual_sol = event.virtual_sol
        if event.virtual_token and event.virtual_token > 0:
            coin.virtual_token = event.virtual_token
        if event.real_token and event.real_token > 0:
            coin.real_token = event.real_token
        if coin.virtual_sol <= 0:
            coin.virtual_sol = INITIAL_VIRTUAL_SOL
        if coin.virtual_token <= 0:
            coin.virtual_token = INITIAL_VIRTUAL_TOKEN
        if coin.real_token <= 0:
            coin.real_token = INITIAL_REAL_TOKEN
        if coin.virtual_sol > 0 and coin.virtual_token > 0:
            coin.price_sol = coin.virtual_sol / coin.virtual_token
            if coin.first_price_sol <= 0:
                coin.first_price_sol = coin.price_sol
        coin.market_cap_sol = coin.price_sol * coin.total_supply
        coin.last_ts = now
        coin.snapshot(now)
        self.creates.append(event)
        self.stats.creates += 1
        return coin

    def apply_complete(self, event: CompleteEvent) -> Coin:
        now = event.seen_ts or time.time()
        coin = self.get_or_create(event.mint, now)
        coin.graduated = True
        coin.graduated_ts = float(event.timestamp or now)
        coin.last_ts = now
        if event.bonding_curve:
            coin.bonding_curve = event.bonding_curve
        self.completes.append(event)
        self.stats.completes += 1
        return coin

    def set_metadata(self, mint: str, metadata: dict[str, Any]) -> None:
        coin = self.coins.get(mint)
        if coin is not None:
            coin.metadata.update(metadata)

    def set_sol_usd(self, price: float, *, source: str = "", now: float | None = None) -> None:
        if price <= 0:
            return
        self.sol_usd = price
        self.sol_price_source = source
        self.sol_price_ts = time.time() if now is None else now

    # ── reads ───────────────────────────────────────────────────────────
    def get(self, mint: str) -> Coin | None:
        return self.coins.get(mint)

    def count(self) -> int:
        return len(self.coins)

    def graduated_count(self) -> int:
        return sum(1 for coin in self.coins.values() if coin.graduated)

    def top(
        self,
        limit: int = 50,
        *,
        sort: str = "mcap",
        query: str = "",
        graduated: bool | None = None,
        min_mcap: float = 0.0,
    ) -> list[Coin]:
        now = time.time()
        query = query.strip().lower()
        rows: list[Coin] = []
        for coin in self.coins.values():
            if graduated is not None and coin.graduated != graduated:
                continue
            if min_mcap > 0:
                mcap_usd = coin.market_cap_sol * self.sol_usd
                if self.sol_usd > 0 and mcap_usd < min_mcap:
                    continue
                if self.sol_usd <= 0 and coin.market_cap_sol < min_mcap:
                    continue
            if query and not (
                query in coin.mint.lower()
                or query in coin.name.lower()
                or query in coin.symbol.lower()
            ):
                continue
            rows.append(coin)
        if sort in {"new", "age"}:
            rows.sort(key=lambda c: c.created_ts or c.first_seen_ts, reverse=True)
        elif sort == "volume":
            rows.sort(key=lambda c: c.volume_sol, reverse=True)
        elif sort in {"gainers", "change"}:
            rows.sort(key=lambda c: c.change_window(300, now), reverse=True)
        elif sort == "trades":
            rows.sort(key=lambda c: c.trade_count, reverse=True)
        elif sort == "price":
            rows.sort(key=lambda c: c.price_sol, reverse=True)
        elif sort == "curve":
            rows.sort(key=lambda c: c.curve_progress_pct(), reverse=True)
        else:
            rows.sort(key=lambda c: c.market_cap_sol, reverse=True)
        return rows[: max(0, limit)]

    def top_dicts(self, limit: int = 50, **kwargs: Any) -> list[dict[str, Any]]:
        now = time.time()
        return [
            coin.to_summary_dict(sol_usd=self.sol_usd, now=now)
            for coin in self.top(limit, **kwargs)
        ]

    def trades_for(self, mint: str, limit: int = 100) -> list[TradeEvent]:
        coin = self.coins.get(mint)
        if coin is not None:
            return list(coin.recent)[-limit:][::-1]
        return [event for event in reversed(self.trades) if event.mint == mint][:limit]

    def recent_trades(
        self,
        limit: int = 100,
        *,
        mint: str | None = None,
        side: str | None = None,
        min_sol: float = 0.0,
    ) -> list[TradeEvent]:
        out: list[TradeEvent] = []
        for event in reversed(self.trades):
            if mint and event.mint != mint:
                continue
            if side == "buy" and not event.is_buy:
                continue
            if side == "sell" and event.is_buy:
                continue
            if event.sol < min_sol:
                continue
            out.append(event)
            if len(out) >= limit:
                break
        return out

    def recent_creates(self, limit: int = 50) -> list[CreateEvent]:
        return list(self.creates)[-limit:][::-1]

    def recent_completes(self, limit: int = 50) -> list[CompleteEvent]:
        return list(self.completes)[-limit:][::-1]

    def stats_dict(self) -> dict[str, Any]:
        return self.stats.as_dict(
            coins=len(self.coins),
            graduated=self.graduated_count(),
            sol_usd=self.sol_usd,
        )

    def snapshot(
        self,
        *,
        coins: int = 50,
        trades: int = 50,
        creates: int = 20,
    ) -> dict[str, Any]:
        now = time.time()
        return {
            "type": "snapshot",
            "ts": round(now, 3),
            "stats": self.stats_dict(),
            "sol_price": {
                "usd": round(self.sol_usd, 4),
                "source": self.sol_price_source,
                "age_s": round(now - self.sol_price_ts, 1) if self.sol_price_ts else None,
            },
            "coins": self.top_dicts(coins),
            "trades": [event.as_dict() for event in self.recent_trades(trades)],
            "creates": [event.as_dict() for event in self.recent_creates(creates)],
            "completes": [event.as_dict() for event in self.recent_completes(10)],
            "metadata": dict(self.metadata_stats),
        }


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)
