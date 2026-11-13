"""HTTP + WebSocket API and the single-page dashboard."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from .demo import DemoFeed
from .firehose import Firehose
from .models import CoinBook

if TYPE_CHECKING:
    from .metadata import MetadataFetcher
    from .solprice import SolPrice
    from .store import SQLiteStore

_WEB_DIR = Path(__file__).parent / "web"


def create_app(
    book: CoinBook,
    *,
    firehose: Firehose | None = None,
    solprice: SolPrice | None = None,
    store: SQLiteStore | None = None,
    metadata: MetadataFetcher | None = None,
    demo: DemoFeed | None = None,
    title: str = "PumpFire",
    serve_ui: bool = True,
) -> FastAPI:
    """Build the FastAPI app around a live :class:`CoinBook`.

    ``serve_ui=False`` keeps the HTTP/WebSocket API but skips the dashboard, so
    the frontend is never compulsory.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        tasks: list[asyncio.Task[Any]] = []
        if solprice is not None:
            await solprice.start()
        if store is not None:
            await store.start()
            if firehose is not None:
                firehose.add_listener(lambda event: _store_event(store, event))
            if demo is not None:
                demo.on_event = lambda event: _store_event(store, event)
        if firehose is not None:
            await firehose.start()
        elif metadata is not None:
            await metadata.start()
        if demo is not None:
            await demo.start()
            tasks.append(asyncio.create_task(demo.run(), name="pumpfire-demo"))
        if store is not None:
            tasks.append(asyncio.create_task(_snapshot_coins(store, book), name="pumpfire-coins"))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if demo is not None:
                await demo.stop()
            if firehose is not None:
                await firehose.stop()
            if solprice is not None:
                await solprice.stop()
            if firehose is None and metadata is not None:
                await metadata.stop()
            if store is not None:
                await store.stop()

    app = FastAPI(
        title=title,
        version="0.1.0",
        description=(
            "Every pump.fun trade, decoded from the program's own Anchor events. "
            "No API keys, no scraper, no RPC round-trip."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    # ── health ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["system"])
    async def health(response: Response) -> dict[str, Any]:
        if demo is not None:
            return {"ok": True, "mode": "demo", "ts": round(time.time(), 3)}
        if firehose is None:
            return {"ok": True, "mode": "book-only", "ts": round(time.time(), 3)}
        stream = firehose.health()
        ok = stream.live
        if not ok:
            response.status_code = 503
        return {"ok": ok, "mode": "live", "ts": round(time.time(), 3), "stream": stream.as_dict()}

    @app.get("/v1/stats", tags=["system"])
    async def stats() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stats": book.stats_dict(),
            "sol_price": (
                solprice.health()
                if solprice is not None
                else {
                    "usd": round(book.sol_usd, 4),
                    "source": book.sol_price_source,
                    "age_s": (
                        round(time.time() - book.sol_price_ts, 1)
                        if book.sol_price_ts
                        else None
                    ),
                }
            ),
            "metadata": (
                metadata.health()
                if metadata is not None
                else {"enabled": False, **book.metadata_stats}
            ),
        }
        if firehose is not None:
            payload["stream"] = firehose.health().as_dict()
        if store is not None:
            payload["store"] = store.health()
        return payload

    # ── coins ───────────────────────────────────────────────────────────
    @app.get("/v1/coins", tags=["coins"])
    async def coins(
        limit: int = Query(50, ge=1, le=500),
        sort: str = Query("mcap", description="mcap | volume | gainers | new | trades | curve | price"),
        q: str = Query("", description="search mint, name, or symbol"),
        graduated: bool | None = Query(None),
        min_mcap: float = Query(
            0.0,
            ge=0,
            description="Minimum market cap. USD when a SOL price is available, otherwise SOL.",
        ),
    ) -> dict[str, Any]:
        rows = book.top_dicts(
            limit,
            sort=sort,
            query=q,
            graduated=graduated,
            min_mcap=min_mcap,
        )
        return {"count": len(rows), "coins": rows}

    @app.get("/v1/coins/{mint}", tags=["coins"])
    async def coin(mint: str, trades: int = Query(50, ge=0, le=300)) -> dict[str, Any]:
        found = book.get(mint)
        if found is None:
            raise HTTPException(status_code=404, detail="coin not found in the live book")
        payload = found.to_dict(sol_usd=book.sol_usd)
        payload["trades"] = [event.as_dict() for event in book.trades_for(mint, trades)]
        return payload

    @app.get("/v1/coins/{mint}/trades", tags=["coins"])
    async def coin_trades(mint: str, limit: int = Query(100, ge=1, le=300)) -> dict[str, Any]:
        if book.get(mint) is None:
            raise HTTPException(status_code=404, detail="coin not found in the live book")
        rows = [event.as_dict() for event in book.trades_for(mint, limit)]
        return {"mint": mint, "count": len(rows), "trades": rows}

    # ── tape ────────────────────────────────────────────────────────────
    @app.get("/v1/trades", tags=["tape"])
    async def trades(
        limit: int = Query(100, ge=1, le=1_000),
        mint: str | None = Query(None),
        side: str | None = Query(None, pattern="^(buy|sell)$"),
        min_sol: float = Query(0.0, ge=0),
    ) -> dict[str, Any]:
        rows = [
            event.as_dict()
            for event in book.recent_trades(limit, mint=mint, side=side, min_sol=min_sol)
        ]
        return {"count": len(rows), "trades": rows}

    @app.get("/v1/creates", tags=["tape"])
    async def creates(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        rows = [event.as_dict() for event in book.recent_creates(limit)]
        return {"count": len(rows), "creates": rows}

    @app.get("/v1/completes", tags=["tape"])
    async def completes(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        rows = [event.as_dict() for event in book.recent_completes(limit)]
        return {"count": len(rows), "completes": rows}

    @app.get("/v1/events", tags=["events"])
    async def events(
        limit: int = Query(100, ge=1, le=1_000),
        program: str | None = Query(None, description="pump or pump_amm"),
        event: str | None = Query(None, description="IDL event name, e.g. BuyEvent"),
    ) -> dict[str, Any]:
        rows = [
            item.as_dict()
            for item in book.raw_events_for(limit, program=program, event=event)
        ]
        return {"count": len(rows), "events": rows}

    @app.get("/v1/pools", tags=["events"])
    async def pools() -> dict[str, Any]:
        rows = [
            {
                "pool": info.pool,
                "base_mint": info.base_mint,
                "base_decimals": info.base_decimals,
                "quote_mint": info.quote_mint,
                "quote_decimals": info.quote_decimals,
                "created_ts": round(info.created_ts, 3),
            }
            for info in book.pools.values()
        ]
        return {"count": len(rows), "pools": rows}

    # ── websocket ───────────────────────────────────────────────────────
    @app.websocket("/ws")
    async def websocket(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(
                    book.snapshot(coins=60, trades=80, creates=30)
                )
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            return

    # ── dashboard ───────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        if not serve_ui:
            return HTMLResponse(
                "<h1>PumpFire API</h1>"
                "<p>The dashboard is disabled. Use <a href='/docs'>/docs</a> "
                "or the <code>/v1/*</code> endpoints.</p>"
            )
        path = _WEB_DIR / "index.html"
        if not path.exists():
            return HTMLResponse("<h1>PumpFire</h1><p>Dashboard asset is missing.</p>", status_code=500)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app


def _store_event(store: SQLiteStore, event: Any) -> None:
    store.enqueue_event(event)


async def _snapshot_coins(store: SQLiteStore, book: CoinBook, interval_s: float = 10.0) -> None:
    while True:
        await asyncio.sleep(interval_s)
        for coin in list(book.coins.values()):
            store.enqueue_coin(coin)
