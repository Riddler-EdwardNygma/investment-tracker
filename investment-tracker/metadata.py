"""Ticker metadata: sector and geographic region.

Fetches from yfinance and caches in ticker_metadata table (7-day TTL).
Users can always override via the transaction form.
"""

from datetime import datetime, timedelta

import yfinance as yf

from db import get_db

METADATA_TTL_DAYS = 7

# Map yfinance country strings → geographic regions
COUNTRY_TO_REGION = {
    # North America
    "United States": "North America",
    "Canada": "North America",
    # Latin America
    "Mexico": "Latin America",
    "Brazil": "Latin America",
    "Argentina": "Latin America",
    "Chile": "Latin America",
    "Colombia": "Latin America",
    "Peru": "Latin America",
    # Europe
    "United Kingdom": "Europe",
    "Germany": "Europe",
    "France": "Europe",
    "Netherlands": "Europe",
    "Switzerland": "Europe",
    "Austria": "Europe",
    "Czech Republic": "Europe",
    "Slovakia": "Europe",
    "Poland": "Europe",
    "Sweden": "Europe",
    "Denmark": "Europe",
    "Norway": "Europe",
    "Finland": "Europe",
    "Italy": "Europe",
    "Spain": "Europe",
    "Portugal": "Europe",
    "Belgium": "Europe",
    "Luxembourg": "Europe",
    "Ireland": "Europe",
    "Greece": "Europe",
    "Hungary": "Europe",
    "Romania": "Europe",
    # Asia-Pacific
    "Japan": "Asia-Pacific",
    "China": "Asia-Pacific",
    "Hong Kong": "Asia-Pacific",
    "South Korea": "Asia-Pacific",
    "Taiwan": "Asia-Pacific",
    "Australia": "Asia-Pacific",
    "New Zealand": "Asia-Pacific",
    "India": "Asia-Pacific",
    "Singapore": "Asia-Pacific",
    "Indonesia": "Asia-Pacific",
    "Thailand": "Asia-Pacific",
    "Malaysia": "Asia-Pacific",
    "Philippines": "Asia-Pacific",
    "Vietnam": "Asia-Pacific",
    # Middle East & Africa
    "South Africa": "Middle East & Africa",
    "Nigeria": "Middle East & Africa",
    "Kenya": "Middle East & Africa",
    "Egypt": "Middle East & Africa",
    "Saudi Arabia": "Middle East & Africa",
    "United Arab Emirates": "Middle East & Africa",
    "Israel": "Middle East & Africa",
    "Qatar": "Middle East & Africa",
    "Kuwait": "Middle East & Africa",
    "Turkey": "Middle East & Africa",
}


def get_metadata(ticker: str) -> dict:
    """Return sector and geography for a ticker.

    Checks the DB cache first. On miss or stale (>7 days), fetches from
    yfinance and upserts the cache.

    Returns dict: {"sector": str, "geography": str}
    """
    ticker = ticker.upper()
    cached = _get_cached(ticker)
    if cached:
        return cached

    # Fetch from yfinance
    sector = ""
    geography = ""
    try:
        info = yf.Ticker(ticker).info
        sector = info.get("sector") or ""
        country = info.get("country") or ""
        geography = COUNTRY_TO_REGION.get(country, "Other" if country else "")
    except Exception:
        pass

    _upsert(ticker, sector, geography)
    return {"sector": sector, "geography": geography}


def upsert_metadata(ticker: str, sector: str, geography: str):
    """Manually set sector and geography for a ticker (from the form)."""
    _upsert(ticker.upper(), sector or "", geography or "")


def _get_cached(ticker: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT sector, geography, last_updated FROM ticker_metadata WHERE ticker=?",
        (ticker,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    # If both fields are empty the previous fetch failed (e.g. SSL issue) – retry
    if not row["sector"] and not row["geography"]:
        return None
    # Check TTL
    try:
        updated = datetime.fromisoformat(row["last_updated"])
        if datetime.utcnow() - updated > timedelta(days=METADATA_TTL_DAYS):
            return None
    except Exception:
        return None
    return {"sector": row["sector"] or "", "geography": row["geography"] or ""}


def _upsert(ticker: str, sector: str, geography: str):
    conn = get_db()
    conn.execute(
        """INSERT INTO ticker_metadata (ticker, sector, geography, last_updated)
           VALUES (?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET
               sector=excluded.sector,
               geography=excluded.geography,
               last_updated=excluded.last_updated""",
        (ticker, sector, geography, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
