"""Symbol normalisation and price retrieval (Yahoo Finance via yfinance)."""
from __future__ import annotations
import logging, re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Optional

log = logging.getLogger("stackwealth.market")
# Configurable exchange suffix mapping (Yahoo Finance convention).
EXCHANGE_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}
DEFAULT_EXCHANGE = "NSE"
_SYM = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,24}$")
STALE_AFTER_DAYS = 4  # covers weekends + one holiday


def normalize_symbol(raw: str, default_exchange: str = DEFAULT_EXCHANGE) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Stock symbol is required.")
    s = raw.strip().upper().replace(" ", "")
    exch = default_exchange
    if ":" in s:
        pfx, s = s.split(":", 1)
        if pfx not in EXCHANGE_SUFFIX:
            raise ValueError(f"Unsupported exchange prefix '{pfx}'. Use NSE or BSE.")
        exch = pfx
    for ex, suf in EXCHANGE_SUFFIX.items():
        if s.endswith(suf):
            s, exch = s[: -len(suf)], ex
            break
    if not _SYM.match(s):
        raise ValueError(f"Invalid stock symbol '{raw.strip()[:30]}'.")
    return s + EXCHANGE_SUFFIX[exch]


def display_symbol(canonical: str) -> str:
    return canonical.rsplit(".", 1)[0]


@dataclass
class PriceQuote:
    symbol: str
    price: Decimal
    price_date: date


@dataclass
class RefreshResult:
    quotes: list = field(default_factory=list)
    failed: dict = field(default_factory=dict)    # symbol -> reason (error/rate limit)
    no_data: list = field(default_factory=list)   # symbol with empty response

    def summary(self) -> dict:
        return {"refreshed": len(self.quotes), "failed": len(self.failed), "no_data": len(self.no_data)}


class NoData(Exception):
    pass


def _yf_fetch_one(symbol: str) -> PriceQuote:
    import yfinance as yf
    hist = yf.Ticker(symbol).history(period="7d", interval="1d", auto_adjust=False)
    if hist is None or hist.empty or "Close" not in hist:
        raise NoData(symbol)
    hist = hist.dropna(subset=["Close"])
    if hist.empty:
        raise NoData(symbol)
    last = hist.iloc[-1]
    price = Decimal(str(round(float(last["Close"]), 4)))
    if price <= 0:
        raise NoData(symbol)
    return PriceQuote(symbol, price, hist.index[-1].date())


def fetch_prices(symbols, fetcher: Callable[[str], PriceQuote] = _yf_fetch_one, max_workers: int = 4) -> RefreshResult:
    """Fetch distinct symbols with bounded concurrency. Never raises for per-symbol errors."""
    res = RefreshResult()
    uniq = sorted(set(symbols))

    def run(sym):
        try:
            return sym, fetcher(sym), None
        except NoData:
            return sym, None, "nodata"
        except Exception as e:  # network, rate limit, parsing
            log.warning("price fetch failed for %s: %s", sym, type(e).__name__)
            return sym, None, f"{type(e).__name__}"

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        for sym, q, err in ex.map(run, uniq):
            if q is not None:
                res.quotes.append(q)
            elif err == "nodata":
                res.no_data.append(sym)
            else:
                res.failed[sym] = err
    return res


def freshness(price_date: Optional[date], today: Optional[date] = None) -> str:
    if price_date is None:
        return "Missing"
    today = today or date.today()
    return "Stale" if (today - price_date).days > STALE_AFTER_DAYS else "Fresh"
