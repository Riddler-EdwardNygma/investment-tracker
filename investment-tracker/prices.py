import sqlite3
from datetime import datetime, timedelta

import yfinance as yf

from db import get_db

CACHE_TTL_HOURS = 1


def get_price(ticker: str) -> dict | None:
    """Fetch current price for ticker, using cache when fresh.

    Returns dict: {price, currency} or None on failure.
    """
    cached = _get_cached(ticker)
    if cached:
        return cached

    try:
        info = yf.Ticker(ticker).fast_info
        price = info.last_price
        currency = getattr(info, "currency", "USD") or "USD"
        if price:
            _set_cache(ticker, price, currency)
            return {"price": price, "currency": currency}
    except Exception:
        pass

    # Fall back to stale cache if available
    return _get_cached(ticker, ignore_ttl=True)


def refresh_prices(tickers: list[str]) -> dict:
    """Force-refresh prices for a list of tickers. Returns mapping ticker→price."""
    _clear_cache(tickers)
    return {t: get_price(t) for t in tickers}


def _get_cached(ticker: str, ignore_ttl: bool = False) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT price, currency, last_updated FROM price_cache WHERE ticker=?",
        (ticker,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    if not ignore_ttl:
        updated = datetime.fromisoformat(row["last_updated"])
        if datetime.utcnow() - updated > timedelta(hours=CACHE_TTL_HOURS):
            return None
    return {"price": row["price"], "currency": row["currency"]}


def _set_cache(ticker: str, price: float, currency: str):
    conn = get_db()
    conn.execute(
        """INSERT INTO price_cache (ticker, price, currency, last_updated)
           VALUES (?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET
               price=excluded.price,
               currency=excluded.currency,
               last_updated=excluded.last_updated""",
        (ticker, price, currency, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def _clear_cache(tickers: list[str]):
    conn = get_db()
    conn.execute(
        f"DELETE FROM price_cache WHERE ticker IN ({','.join('?'*len(tickers))})",
        tickers
    )
    conn.commit()
    conn.close()
