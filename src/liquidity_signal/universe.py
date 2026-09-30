"""Active markets; archived records may contain retired symbols."""

ACTIVE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "NEARUSDT")
ACTIVE_SYMBOLS_CSV = ",".join(ACTIVE_SYMBOLS)


def training_symbols(symbols: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
    requested = ACTIVE_SYMBOLS if symbols is None else symbols
    selected = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in requested))
    if not selected or any(s not in ACTIVE_SYMBOLS for s in selected):
        raise ValueError(f"Training symbols must be a nonempty subset of {ACTIVE_SYMBOLS_CSV}")
    return selected
