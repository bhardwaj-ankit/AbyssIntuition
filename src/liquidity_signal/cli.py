from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from liquidity_signal.service.engine import SignalEngine

app = typer.Typer(help="Binance liquidity signal CLI")
console = Console()


@app.command()
def signal(symbol: str = "BTCUSDT") -> None:
    engine = SignalEngine()
    result = engine.generate_signal(symbol.upper())

    table = Table(title=f"Signal for {result.symbol}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Direction", result.direction.value)
    table.add_row("Confidence", f"{result.confidence:.2f}")
    table.add_row("Current Price", f"{result.current_price:.2f}")
    table.add_row("TP", f"{result.tp:.2f}")
    table.add_row("SL", f"{result.sl:.2f}")
    table.add_row("Reasons", ", ".join(result.reasons))

    console.print(table)


if __name__ == "__main__":
    app()
