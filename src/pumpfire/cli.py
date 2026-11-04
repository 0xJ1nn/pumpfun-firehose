"""The ``pumpfire`` command line."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .constants import PUMP_PROGRAM
from .decoder import decode_log_line, decode_logs
from .demo import DemoFeed
from .firehose import Firehose
from .metadata import MetadataFetcher
from .models import CoinBook
from .solprice import SolPrice
from .store import SQLiteStore, export_table, table_counts

app = typer.Typer(
    name="pumpfire",
    help="Every pump.fun trade, decoded straight from the chain. Zero API keys.",
    add_completion=False,
)
console = Console()


# ── shared helpers ─────────────────────────────────────────────────────
def _endpoints(value: str | None) -> list[str] | None:
    if not value:
        env = os.environ.get("PUMPFIRE_WS", "")
        value = env
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _fixed_price(value: float) -> float | None:
    if value and value > 0:
        return value
    env = os.environ.get("PUMPFIRE_SOL_USD", "")
    try:
        parsed = float(env)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _read_watchlist(path: Path | None) -> set[str]:
    if path is None:
        return set()
    if not path.exists():
        raise typer.BadParameter(f"no such watchlist: {path}")
    mints: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            mints.add(line)
    return mints


def _matches_event(
    event: Any,
    *,
    mints: set[str],
    min_sol: float,
    buys_only: bool,
    sells_only: bool,
) -> bool:
    event_mint = getattr(event, "mint", None)
    if mints and event_mint not in mints:
        return False
    if event.kind != "trade":
        return True
    if min_sol and event.sol < min_sol:
        return False
    if buys_only and not event.is_buy:
        return False
    return not (sells_only and event.is_buy)


def _build_book(max_coins: int) -> CoinBook:
    return CoinBook(max_coins=max_coins)


def _build_metadata(book: CoinBook, enabled: bool) -> MetadataFetcher | None:
    if not enabled:
        return None
    return MetadataFetcher(book, enabled=True)


def _build_firehose(
    book: CoinBook,
    *,
    ws: str | None,
    commitment: str,
    metadata: MetadataFetcher | None,
    on_event: Any = None,
    programs: list[str] | None = None,
) -> Firehose:
    return Firehose(
        book,
        endpoints=_endpoints(ws),
        commitment=commitment,
        metadata=metadata,
        on_event=on_event,
        programs=programs,
    )


def _fmt_price(value: float | None) -> str:
    if not value or value <= 0:
        return "—"
    if value >= 1:
        return f"{value:.4f}"
    if value >= 0.01:
        return f"{value:.6f}"
    if value >= 0.0001:
        return f"{value:.8f}"
    return f"{value:.2e}"


def _fmt_usd(value: float | None) -> str:
    if not value or value <= 0:
        return "—"
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:.2f}M"
    if value >= 1e3:
        return f"${value / 1e3:.2f}K"
    if value >= 1:
        return f"${value:.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.2e}"


def _fmt_age(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def _render_dashboard(
    book: CoinBook,
    firehose: Firehose,
    *,
    limit: int = 15,
) -> Layout:
    stats = book.stats_dict()
    live = firehose.connected and not firehose.is_stale()
    status_text = "LIVE" if live else "WAITING"
    status_style = "bold green" if live else "bold yellow"
    header = Panel(
        Text.assemble(
            ("🔥 PUMPFIRE  ", "bold"),
            (status_text, status_style),
            (
                f"   {stats['messages_per_sec']:.0f} msg/s   "
                f"{stats['trades_per_sec']:.1f} trades/s   "
                f"{stats['coins']} coins   "
                f"{stats['volume_sol']:.1f} SOL   "
                f"${stats['sol_usd']:.2f}",
                "bright_white",
            ),
        ),
        border_style="green" if live else "yellow",
        padding=(0, 1),
    )

    coins = Table(expand=True, box=None, padding=(0, 1), show_edge=False)
    coins.add_column("#", justify="right", style="bright_black", width=3)
    coins.add_column("Coin", ratio=3)
    coins.add_column("Price", justify="right")
    coins.add_column("MCap", justify="right")
    coins.add_column("5m", justify="right")
    coins.add_column("Vol", justify="right")
    coins.add_column("Curve", justify="right")
    coins.add_column("Age", justify="right")
    now = time.time()
    for index, coin in enumerate(book.top(limit, sort="mcap"), start=1):
        change = coin.change_window(300, now)
        change_style = "green" if change > 0 else "red" if change < 0 else "bright_black"
        mcap = coin.market_cap_sol * book.sol_usd if book.sol_usd else 0
        coins.add_row(
            str(index),
            f"{coin.symbol or coin.name or coin.mint[:6]}  [bright_black]{coin.mint[:4]}…{coin.mint[-4:]}[/]",
            _fmt_price(coin.price_sol),
            _fmt_usd(mcap) if mcap else f"{coin.market_cap_sol:.1f}◎",
            f"[{change_style}]{change:+.1f}%[/]",
            f"{coin.volume_sol:.1f}◎",
            f"{coin.curve_progress_pct():.0f}%",
            _fmt_age(coin.age_s(now)),
        )

    tape = Table(expand=True, box=None, padding=(0, 1), show_edge=False)
    tape.add_column("Time", style="bright_black", width=8)
    tape.add_column("Side", width=5)
    tape.add_column("Coin", ratio=2)
    tape.add_column("SOL", justify="right")
    tape.add_column("Price", justify="right")
    for trade in book.recent_trades(18):
        tape_coin = book.get(trade.mint)
        symbol = (tape_coin.symbol or tape_coin.name) if tape_coin else trade.mint[:6]
        stamp = time.strftime("%H:%M:%S", time.localtime(trade.timestamp or trade.seen_ts))
        side = "[green]BUY [/]" if trade.is_buy else "[magenta]SELL[/]"
        tape.add_row(
            stamp,
            side,
            symbol or "—",
            f"{trade.sol:.3f}",
            _fmt_price(trade.price_sol),
        )

    layout = Layout()
    layout.split_column(
        Layout(header, name="header", size=3),
        Layout(name="body"),
    )
    layout["body"].split_row(
        Layout(Panel(coins, title="[bold]TOP COINS[/]", border_style="bright_black"), name="coins", ratio=2),
        Layout(Panel(tape, title="[bold]LIVE TRADES[/]", border_style="bright_black"), name="tape", ratio=1),
    )
    return layout


async def _wait(seconds: float) -> None:
    if seconds <= 0:
        while True:
            await asyncio.sleep(3600)
    await asyncio.sleep(seconds)


# ── stream ─────────────────────────────────────────────────────────────
@app.command()
def stream(
    ws: Annotated[str | None, typer.Option(help="Comma-separated WSS endpoints.")] = None,
    commitment: Annotated[
        str,
        typer.Option(help="processed, confirmed, or finalized."),
    ] = os.environ.get("PUMPFIRE_COMMITMENT", "processed"),
    min_sol: Annotated[
        float,
        typer.Option("--min-sol", help="Only show trades at or above this SOL size."),
    ] = _env_float("PUMPFIRE_MIN_SOL", 0.0),
    buys_only: Annotated[bool, typer.Option("--buys-only", help="Only show buys.")] = False,
    sells_only: Annotated[bool, typer.Option("--sells-only", help="Only show sells.")] = False,
    mint: Annotated[
        list[str] | None,
        typer.Option("--mint", "-m", help="Only show these mints (repeatable)."),
    ] = None,
    watchlist: Annotated[
        Path | None,
        typer.Option("--watchlist", help="File with one mint per line (# comments allowed)."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSONL instead of the TUI.")] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write JSONL to a file (requires --json)."),
    ] = None,
    duration: Annotated[float, typer.Option(help="Stop after N seconds (0 = forever).")] = 0.0,
    limit: Annotated[int, typer.Option(help="Rows in the TUI.")] = 15,
    metadata: Annotated[
        bool | None,
        typer.Option("--metadata/--no-metadata", help="Fetch token metadata (off by default)."),
    ] = None,
    sol_usd: Annotated[
        float,
        typer.Option("--sol-usd", help="Pin SOL/USD instead of fetching it."),
    ] = 0.0,
    no_amm: Annotated[
        bool,
        typer.Option("--no-amm", help="Skip PumpSwap AMM events; watch the bonding curve only."),
    ] = False,
) -> None:
    """Watch the firehose in your terminal."""
    if out is not None and not as_json:
        raise typer.BadParameter("--out requires --json")
    mints = set(mint or []) | _read_watchlist(watchlist)
    book = _build_book(10_000)
    metadata_enabled = (
        _env_bool("PUMPFIRE_METADATA", False) if metadata is None else metadata
    )
    metadata_fetcher = _build_metadata(book, metadata_enabled)
    fixed_price = _fixed_price(sol_usd)
    solprice = None
    if fixed_price is not None:
        solprice = SolPrice(book, fixed=fixed_price)
    elif not as_json:
        solprice = SolPrice(book)

    output = sys.stdout
    close_output = False
    if as_json and out is not None and str(out) != "-":
        output = out.open("a", encoding="utf-8")
        close_output = True

    def matches(event: Any) -> bool:
        return _matches_event(
            event,
            mints=mints,
            min_sol=min_sol,
            buys_only=buys_only,
            sells_only=sells_only,
        )

    async def on_event(event: Any) -> None:
        if as_json and matches(event):
            print(json.dumps(event.as_dict(), separators=(",", ":")), file=output, flush=True)

    firehose = _build_firehose(
        book,
        ws=ws,
        commitment=commitment,
        metadata=metadata_fetcher,
        on_event=on_event if as_json else None,
        programs=[PUMP_PROGRAM] if no_amm else None,
    )

    async def run() -> None:
        if solprice is not None:
            await solprice.start()
        await firehose.start()
        try:
            if as_json:
                await _wait(duration)
            else:
                with Live(
                    _render_dashboard(book, firehose, limit=limit),
                    refresh_per_second=4,
                    screen=False,
                ) as live:
                    started = time.time()
                    while duration <= 0 or time.time() - started < duration:
                        live.update(_render_dashboard(book, firehose, limit=limit))
                        await asyncio.sleep(0.25)
        finally:
            await firehose.stop()
            if solprice is not None:
                await solprice.stop()
            if close_output:
                output.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


# ── api ────────────────────────────────────────────────────────────────
@app.command()
def api(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8000,
    ws: Annotated[str | None, typer.Option(help="Comma-separated WSS endpoints.")] = None,
    commitment: Annotated[
        str,
        typer.Option(help="processed, confirmed, or finalized."),
    ] = os.environ.get("PUMPFIRE_COMMITMENT", "processed"),
    db: Annotated[Path | None, typer.Option(help="Persist trades/coins to SQLite.")] = None,
    no_metadata: Annotated[bool, typer.Option("--no-metadata", help="Skip token metadata fetches.")] = False,
    sol_usd: Annotated[float, typer.Option("--sol-usd", help="Pin SOL/USD instead of fetching it.")] = 0.0,
    demo: Annotated[bool, typer.Option(help="Serve simulated data (no network).")] = False,
    no_ui: Annotated[
        bool,
        typer.Option("--no-ui", help="Disable the dashboard; serve the API only."),
    ] = False,
    no_amm: Annotated[
        bool,
        typer.Option("--no-amm", help="Skip PumpSwap AMM events; bonding curve only."),
    ] = False,
    max_coins: Annotated[int, typer.Option(help="Maximum coins kept in memory.")] = 10_000,
) -> None:
    """Serve the dashboard, HTTP API, and WebSocket."""
    try:
        import uvicorn

        from .api import create_app
    except ImportError as exc:
        console.print(
            "[red]The API needs optional dependencies.[/]\n"
            "Install with: [bold]pip install 'pumpfun-firehose[api]'[/]"
        )
        raise typer.Exit(code=1) from exc

    if db is None and os.environ.get("PUMPFIRE_DB"):
        db = Path(os.environ["PUMPFIRE_DB"])
    book = _build_book(max_coins)
    if demo:
        demo_feed: DemoFeed | None = DemoFeed(book)
        firehose = None
        solprice = None
        metadata = None
    else:
        demo_feed = None
        metadata_enabled = _env_bool("PUMPFIRE_METADATA", True) and not no_metadata
        metadata = _build_metadata(book, metadata_enabled)
        firehose = _build_firehose(
            book,
            ws=ws,
            commitment=commitment,
            metadata=metadata,
            programs=[PUMP_PROGRAM] if no_amm else None,
        )
        solprice = SolPrice(book, fixed=_fixed_price(sol_usd))
    store = SQLiteStore(db) if db is not None else None
    application = create_app(
        book,
        firehose=firehose,
        solprice=solprice,
        store=store,
        metadata=metadata,
        demo=demo_feed,
        serve_ui=not no_ui,
    )
    console.print(
        f"[bold green]PumpFire[/] on [bold]http://{host}:{port}[/]"
        f"{'  [yellow](demo)[/]' if demo else ''}"
    )
    uvicorn.run(application, host=host, port=port, log_level="info")


# ── top ────────────────────────────────────────────────────────────────
@app.command(name="top")
def top(
    duration: Annotated[float, typer.Option(help="Seconds to listen before printing.")] = 30.0,
    limit: Annotated[int, typer.Option(help="Rows to print.")] = 20,
    sort: Annotated[str, typer.Option(help="mcap | volume | gainers | new | trades | curve")] = "mcap",
    ws: Annotated[str | None, typer.Option(help="Comma-separated WSS endpoints.")] = None,
    commitment: Annotated[
        str,
        typer.Option(help="processed, confirmed, or finalized."),
    ] = os.environ.get("PUMPFIRE_COMMITMENT", "processed"),
    sol_usd: Annotated[float, typer.Option("--sol-usd", help="Pin SOL/USD instead of fetching it.")] = 0.0,
) -> None:
    """Listen for a while, then print a ranked snapshot."""
    book = _build_book(20_000)
    solprice = SolPrice(book, fixed=_fixed_price(sol_usd))
    firehose = _build_firehose(book, ws=ws, commitment=commitment, metadata=None)

    async def run() -> None:
        await solprice.start()
        await firehose.start()
        try:
            with console.status("[green]listening to the firehose…[/]"):
                await asyncio.sleep(duration)
        finally:
            await firehose.stop()
            await solprice.stop()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())

    table = Table(title=f"PumpFire — top {limit} by {sort}", box=None, padding=(0, 1))
    table.add_column("#", justify="right", style="bright_black")
    table.add_column("Coin")
    table.add_column("Mint", style="bright_black")
    table.add_column("Price", justify="right")
    table.add_column("MCap", justify="right")
    table.add_column("5m", justify="right")
    table.add_column("Vol", justify="right")
    table.add_column("Curve", justify="right")
    for index, coin in enumerate(book.top(limit, sort=sort), start=1):
        change = coin.change_window(300)
        style = "green" if change > 0 else "red" if change < 0 else "bright_black"
        mcap = coin.market_cap_sol * book.sol_usd if book.sol_usd else 0
        table.add_row(
            str(index),
            coin.symbol or coin.name or "—",
            f"{coin.mint[:6]}…{coin.mint[-4:]}",
            _fmt_price(coin.price_sol),
            _fmt_usd(mcap) if mcap else f"{coin.market_cap_sol:.1f}◎",
            f"[{style}]{change:+.1f}%[/]",
            f"{coin.volume_sol:.1f}◎",
            f"{coin.curve_progress_pct():.0f}%",
        )
    console.print(table)
    stats = book.stats_dict()
    console.print(
        f"[dim]{stats['trades']} trades · {stats['creates']} launches · "
        f"{stats['coins']} coins · {stats['volume_sol']:.1f} SOL · "
        f"{stats['trades_per_sec']:.1f} trades/s[/]"
    )


# ── decode ─────────────────────────────────────────────────────────────
@app.command()
def decode(
    log: Annotated[
        list[str] | None,
        typer.Option("--log", "-l", help="Program data line (repeatable)."),
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option(help="JSON notification or JSONL file."),
    ] = None,
    program: Annotated[
        str | None,
        typer.Option("--program", help="pump or pump_amm, for bare --log lines."),
    ] = None,
    compact: Annotated[bool, typer.Option(help="Print one JSON object per line.")] = False,
) -> None:
    """Decode raw Program data lines or a recorded notification."""
    events: list[Any] = []
    if log:
        for line in log:
            events.extend(decode_log_line(line, program=program).events)
    if file is not None:
        if not file.exists():
            raise typer.BadParameter(f"no such file: {file}")
        text = file.read_text(encoding="utf-8").strip()
        records: list[Any]
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                records = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                records = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
        for record in records:
            if isinstance(record, dict) and "logs" in record:
                events.extend(
                    decode_logs(
                        record.get("logs") or [],
                        signature=record.get("signature") or "",
                        slot=record.get("slot"),
                    ).events
                )
            elif isinstance(record, dict) and "params" in record:
                value = ((record.get("params") or {}).get("result") or {}).get("value") or {}
                events.extend(
                    decode_logs(
                        value.get("logs") or [],
                        signature=value.get("signature") or "",
                        slot=value.get("slot"),
                    ).events
                )
    if not events:
        console.print("[yellow]no pump.fun events found[/]")
        raise typer.Exit(code=1)
    for event in events:
        if compact:
            print(json.dumps(event.as_dict(), separators=(",", ":")))
        else:
            print(json.dumps(event.as_dict(), indent=2))


# ── export ─────────────────────────────────────────────────────────────
@app.command()
def export(
    db: Annotated[
        Path | None,
        typer.Option(help="SQLite database written by `pumpfire api --db`."),
    ] = None,
    table: Annotated[str, typer.Option(help="trades | creates | completes | coins")] = "trades",
    fmt: Annotated[str, typer.Option("--format", help="csv | jsonl")] = "csv",
    out: Annotated[str, typer.Option(help="Output path, or - for stdout.")] = "-",
) -> None:
    """Export a persisted table to CSV or JSONL."""
    if db is None:
        env_db = os.environ.get("PUMPFIRE_DB")
        if not env_db:
            raise typer.BadParameter("--db is required (or set PUMPFIRE_DB)")
        db = Path(env_db)
    if not db.exists():
        raise typer.BadParameter(f"no such database: {db}")
    count = export_table(db, table, fmt=fmt, out=out)
    if out != "-":
        console.print(f"[green]wrote {count} rows[/] to {out}")
    counts = table_counts(db)
    console.print(f"[dim]{counts}[/]")


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"pumpfire {__version__}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
