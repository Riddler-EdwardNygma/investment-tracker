"""Currency conversion using yfinance forex pairs.

Reuses the existing prices.py cache so forex rates benefit from the same
1-hour TTL and SQLite cache as equity prices.
"""

from prices import get_price

# Currencies we support converting between
SUPPORTED = {"CZK", "EUR", "USD", "GBP", "CHF"}


def get_rate(from_currency: str, to_currency: str) -> float:
    """Return exchange rate: 1 from_currency = X to_currency.

    Falls back to 1.0 if the rate cannot be fetched.
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    if from_currency == to_currency:
        return 1.0

    ticker = f"{from_currency}{to_currency}=X"
    data = get_price(ticker)
    if data and data.get("price"):
        return float(data["price"])

    # Try inverse
    ticker_inv = f"{to_currency}{from_currency}=X"
    data_inv = get_price(ticker_inv)
    if data_inv and data_inv.get("price") and data_inv["price"] != 0:
        return 1.0 / float(data_inv["price"])

    return 1.0  # fallback


def convert(amount: float, from_currency: str, to_currency: str) -> float:
    """Convert amount from one currency to another."""
    return amount * get_rate(from_currency, to_currency)
