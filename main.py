"""
main.py
───────
Lykhan Forex Sub-Agent — Command Line Interface

This file lives at the Django project root (lykhan/) and uses the
correct import paths for the Django project structure, where all forex
service modules live under forex/services/.

Run this from the project root with the virtual environment activated:

    cd /home/gmnak2/Desktop/lykhan
    source .venv/bin/activate

Usage examples
──────────────
    python main.py bridge-check          # Check if MT5 EA is alive
    python main.py status                # Print live account snapshot
    python main.py buy                   # Open BUY at default settings
    python main.py sell --lot 0.02       # Open SELL with custom lot size
    python main.py close --ticket 12345  # Close a specific trade by ticket
    python main.py close-all             # Close all open trades
    python main.py monitor               # Live refreshing account monitor
"""

import sys
import os
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

# Ensure the Django project root is on Python's module search path.
# This is what makes `from forex.services...` imports work correctly
# when running this script directly from the command line.
sys.path.insert(0, str(Path(__file__).parent))

# Set the Django settings module so that if any Django components are
# needed they know which settings file to use.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project.settings')

# ── Import forex service layer using Django project paths ─────────────────────
from forex.services.core.trade_executor import TradeExecutor, TradeValidationError
from forex.services.core.account_monitor import AccountMonitor
from forex.services.core.schemas import AccountSnapshot

console = Console()


def get_executor() -> TradeExecutor:
    """Create and return a fresh TradeExecutor instance."""
    return TradeExecutor()


# ── CLI Root Group ────────────────────────────────────────────────────────────

@click.group()
def cli():
    """
    \b
    ██╗      ██╗   ██╗██╗  ██╗██╗  ██╗ █████╗ ███╗   ██╗
    ██║      ╚██╗ ██╔╝██║ ██╔╝██║  ██║██╔══██╗████╗  ██║
    ██║       ╚████╔╝ █████╔╝ ███████║███████║██╔██╗ ██║
    ██║        ╚██╔╝  ██╔═██╗ ██╔══██║██╔══██║██║╚██╗██║
    ███████╗    ██║   ██║  ██╗██║  ██║██║  ██║██║ ╚████║
    ╚══════╝    ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝
    Lykhan Forex Sub-Agent — MT5 Trade Controller
    """
    pass


# ── Commands ──────────────────────────────────────────────────────────────────

@cli.command("bridge-check")
def bridge_check():
    """Check whether the MT5 LykhanBridge EA is alive and writing heartbeats."""
    executor = get_executor()
    alive = executor.check_bridge()
    if alive:
        console.print(Panel(
            "[bold green]✓  MT5 Bridge is ALIVE[/]\n"
            "The LykhanBridge EA is running and writing heartbeats.",
            expand=False
        ))
    else:
        console.print(Panel(
            "[bold red]✗  MT5 Bridge is OFFLINE[/]\n"
            "Ensure LykhanBridge.ex5 is attached to a chart in MT5\n"
            "and that Algo Trading is enabled in the MT5 toolbar.",
            expand=False
        ))
    sys.exit(0 if alive else 1)


@cli.command()
def status():
    """Display a live account snapshot fetched from MT5."""
    executor = get_executor()
    try:
        snap = executor.get_account_snapshot()
        _print_snapshot(snap)
    except Exception as exc:
        console.print(f"[red]Error fetching status: {exc}[/]")
        sys.exit(1)


@cli.command()
@click.option("--symbol",  default=None,  help="Trading symbol (default: EURUSD)")
@click.option("--lot",     default=None,  type=float, help="Lot size")
@click.option("--sl",      default=None,  type=int,   help="Stop loss in pips")
@click.option("--tp",      default=None,  type=int,   help="Take profit in pips")
@click.option("--comment", default="lykhan-buy", help="Trade comment")
def buy(symbol, lot, sl, tp, comment):
    """Open a BUY (long) market position."""
    try:
        result = get_executor().open_buy(
            symbol=symbol, lot_size=lot, sl_pips=sl, tp_pips=tp, comment=comment
        )
        _print_result(result)
    except TradeValidationError as exc:
        console.print(f"[red]Validation error: {exc}[/]")
        sys.exit(1)


@cli.command()
@click.option("--symbol",  default=None,  help="Trading symbol (default: EURUSD)")
@click.option("--lot",     default=None,  type=float, help="Lot size")
@click.option("--sl",      default=None,  type=int,   help="Stop loss in pips")
@click.option("--tp",      default=None,  type=int,   help="Take profit in pips")
@click.option("--comment", default="lykhan-sell", help="Trade comment")
def sell(symbol, lot, sl, tp, comment):
    """Open a SELL (short) market position."""
    try:
        result = get_executor().open_sell(
            symbol=symbol, lot_size=lot, sl_pips=sl, tp_pips=tp, comment=comment
        )
        _print_result(result)
    except TradeValidationError as exc:
        console.print(f"[red]Validation error: {exc}[/]")
        sys.exit(1)


@cli.command()
@click.option("--ticket", required=True, type=int, help="Ticket number to close")
def close(ticket):
    """Close a specific open position by ticket number."""
    result = get_executor().close_trade(ticket)
    _print_result(result)


@cli.command("close-all")
@click.confirmation_option(prompt="This will close ALL open trades. Are you sure?")
def close_all():
    """Close ALL open positions (asks for confirmation first)."""
    result = get_executor().close_all_trades()
    _print_result(result)


@cli.command()
@click.option("--interval", default=10, type=int, help="Refresh interval in seconds")
def monitor(interval):
    """Live account monitor — refreshes every N seconds. Ctrl+C to stop."""
    import time
    console.print(
        f"[bold cyan]Live monitor started (interval={interval}s) — Ctrl+C to stop[/]\n"
    )
    try:
        while True:
            try:
                snap = get_executor().get_account_snapshot()
                console.clear()
                _print_snapshot(snap)
            except Exception as exc:
                console.print(f"[red]Monitor error: {exc}[/]")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Monitor stopped.[/]")


# ── Display Helpers ───────────────────────────────────────────────────────────

def _print_snapshot(snap: AccountSnapshot) -> None:
    """Render a formatted account snapshot panel to the terminal."""
    pnl_colour = "green" if snap.total_floating_pnl >= 0 else "red"
    console.print(Panel(
        f"[bold]Balance:[/]      [green]{snap.balance:>12.2f}[/]\n"
        f"[bold]Equity:[/]       [green]{snap.equity:>12.2f}[/]\n"
        f"[bold]Free Margin:[/]  [cyan]{snap.free_margin:>12.2f}[/]\n"
        f"[bold]Margin Level:[/] [cyan]{snap.margin_level:>11.2f}%[/]\n"
        f"[bold]Floating P&L:[/] [{pnl_colour}]{snap.total_floating_pnl:>+12.2f}[/]\n"
        f"[bold]Open Trades:[/]  {snap.open_trade_count:>12}",
        title="[bold blue]Account Snapshot[/]",
        expand=False,
    ))

    if snap.positions:
        table = Table(
            "Ticket", "Symbol", "Type", "Lots",
            "Open", "Current", "SL", "TP", "Profit", "Comment",
            box=box.ROUNDED,
            title="Open Positions",
        )
        for pos in snap.positions:
            colour = "green" if pos.profit >= 0 else "red"
            table.add_row(
                str(pos.ticket),
                pos.symbol,
                f"[{'green' if pos.action == 'BUY' else 'red'}]{pos.action}[/]",
                f"{pos.lot_size:.2f}",
                f"{pos.open_price:.5f}",
                f"{pos.current_price:.5f}",
                f"{pos.sl:.5f}",
                f"{pos.tp:.5f}",
                f"[{colour}]{pos.profit:+.2f}[/]",
                pos.comment,
            )
        console.print(table)
    else:
        console.print("[dim]No open positions.[/]")


def _print_result(result) -> None:
    """Render a formatted trade result panel to the terminal."""
    colour = "green" if result.status in ("EXECUTED", "CLOSED") else "red"
    console.print(Panel(
        f"[bold]Status:[/]   [{colour}]{result.status}[/]\n"
        f"[bold]Ticket:[/]   {result.ticket}\n"
        f"[bold]Price:[/]    {result.open_price}\n"
        f"[bold]Profit:[/]   {result.profit}\n"
        f"[bold]Error:[/]    {result.error_message or '—'}",
        title="[bold]Trade Result[/]",
        expand=False,
    ))


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
