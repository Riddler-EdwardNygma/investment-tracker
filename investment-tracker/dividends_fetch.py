"""Fetch historical dividend payments from Yahoo Finance.

For each ticker in the portfolio, retrieves the full dividend history,
calculates shares held at each ex-dividend date (using all BUY/SELL
transactions), and returns suggestions not yet recorded in the DB.
"""

import yfinance as yf


def _shares_held_at(ticker: str, date_str: str, transactions: list) -> float:
    """Calculate shares of *ticker* held on *date_str* (FIFO)."""
    ticker = ticker.upper()
    buys = []
    total_sold = 0.0

    for tx in transactions:
        if tx["ticker"].upper() != ticker:
            continue
        if tx["date"] > date_str:
            continue
        tx_type = tx["transaction_type"].upper()
        if tx_type == "BUY":
            buys.append(float(tx["quantity"]))
        elif tx_type == "SELL":
            total_sold += float(tx["quantity"])

    # FIFO: consume oldest lots first
    remaining_sold = total_sold
    total_held = 0.0
    for qty in buys:
        if remaining_sold >= qty:
            remaining_sold -= qty
        else:
            total_held += qty - remaining_sold
            remaining_sold = 0.0

    return round(total_held, 8)


def fetch_market_dividends(
    tickers: list,
    transactions: list,
    existing_dividends: list,
) -> tuple[list, list]:
    """Return (suggestions, errors).

    suggestions – list of dicts for dividends not yet in DB:
        ticker, name, asset_type, date, amount_per_share,
        shares_held, total, currency

    errors – list of (ticker, error_message) for tickers that failed
    """
    # Build dedup set from already-recorded dividends
    existing_set = {
        (str(d["ticker"]).upper(), str(d["date"]))
        for d in existing_dividends
    }

    # Build asset_type lookup from existing BUY transactions
    asset_type_map: dict[str, str] = {}
    name_map: dict[str, str] = {}
    for tx in transactions:
        t = tx["ticker"].upper()
        if tx["transaction_type"].upper() == "BUY":
            asset_type_map.setdefault(t, tx["asset_type"])
            name_map.setdefault(t, tx["name"])

    suggestions = []
    errors = []

    for ticker in tickers:
        ticker_up = ticker.upper()
        try:
            yf_ticker = yf.Ticker(ticker_up)

            # Dividend history (pandas Series, index = tz-aware Timestamp)
            divs = yf_ticker.dividends
            if divs is None or len(divs) == 0:
                continue

            # Currency from fast_info (lightweight, no full .info call needed)
            try:
                currency = yf_ticker.fast_info.get("currency") or "USD"
            except Exception:
                currency = "USD"

            for ts, amount_per_share in divs.items():
                # Normalise to plain YYYY-MM-DD string
                try:
                    date_str = ts.date().isoformat()
                except AttributeError:
                    date_str = str(ts)[:10]

                # Skip already recorded
                if (ticker_up, date_str) in existing_set:
                    continue

                shares = _shares_held_at(ticker_up, date_str, transactions)
                if shares <= 0:
                    continue

                amount = float(amount_per_share)
                total = round(amount * shares, 6)

                suggestions.append({
                    "ticker": ticker_up,
                    "name": name_map.get(ticker_up, ""),
                    "asset_type": asset_type_map.get(ticker_up, "Share"),
                    "date": date_str,
                    "amount_per_share": round(amount, 6),
                    "shares_held": round(shares, 4),
                    "total": total,
                    "currency": currency,
                })

        except Exception as exc:
            errors.append((ticker_up, str(exc)))

    suggestions.sort(key=lambda x: x["date"], reverse=True)
    return suggestions, errors
