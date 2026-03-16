"""XTB CSV import parser.

XTB export format (semicolon-delimited):
    Type;Instrument;Time;Amount;ID;Comment;Product

Supported transaction types:
    Stock purchase  → BUY
    Stock sell      → SELL
    Dividend        → DIVIDEND

All other types (Deposit, Withdrawal, Free funds interest, etc.) are skipped.

NOTE: XTB does not export quantity and price separately — only the total
Amount is available.  Imported rows are therefore created with quantity=1
and price_per_unit=abs(Amount), flagged as needs_review=1 so the user
can correct them afterwards.
"""

import csv
import io
from datetime import datetime

# Map XTB "Type" column values to our transaction types
TYPE_MAP = {
    "Stock purchase": "BUY",
    "Stock sell": "SELL",
    "Dividend": "DIVIDEND",
}

SKIP_TYPES = {
    "Deposit",
    "Withdrawal",
    "Free funds interest",
    "Free funds interest tax",
    "Withholding tax",
    "Swap",
    "Close trade",
    "Subaccount transfer",
    "Total",
}


def parse_xtb_csv(file_stream) -> tuple[list[dict], list[dict]]:
    """Parse an XTB export CSV file.

    Args:
        file_stream: file-like object (binary or text)

    Returns:
        (importable, skipped) – two lists of row dicts.
        importable rows are ready to pass to db.add_transaction(**row).
    """
    # Decode if binary
    if isinstance(file_stream.read(0), bytes):
        content = file_stream.read().decode("utf-8-sig", errors="replace")
    else:
        content = file_stream.read()

    reader = csv.DictReader(io.StringIO(content), delimiter=";")

    importable = []
    skipped = []

    for row in reader:
        raw_type = (row.get("Type") or "").strip()
        tx_type = TYPE_MAP.get(raw_type)

        if tx_type is None:
            skipped.append({"type": raw_type, "reason": "Unsupported type", **_clean_row(row)})
            continue

        try:
            amount = abs(float((row.get("Amount") or "0").replace(",", ".")))
        except ValueError:
            skipped.append({"type": raw_type, "reason": "Invalid amount", **_clean_row(row)})
            continue

        # Parse date
        raw_time = (row.get("Time") or "").strip()
        try:
            tx_date = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S").date().isoformat()
        except ValueError:
            try:
                tx_date = datetime.strptime(raw_time, "%Y-%m-%d").date().isoformat()
            except ValueError:
                skipped.append({"type": raw_type, "reason": "Invalid date", **_clean_row(row)})
                continue

        ticker = (row.get("Instrument") or "").strip().upper()
        if not ticker:
            skipped.append({"type": raw_type, "reason": "No ticker", **_clean_row(row)})
            continue

        importable.append({
            "ticker": ticker,
            "name": ticker,
            "asset_type": "Share",
            "transaction_type": tx_type,
            "quantity": 1.0,
            "price_per_unit": amount,
            "currency": "CZK",  # XTB doesn't include per-trade currency in this format
            "date": tx_date,
            "notes": (row.get("Comment") or "").strip() or f"XTB import #{(row.get('ID') or '').strip()}",
            "hold_years": 3,
            "needs_review": 1,
        })

    return importable, skipped


def _clean_row(row: dict) -> dict:
    return {
        "ticker": (row.get("Instrument") or "").strip(),
        "date": (row.get("Time") or "").strip(),
        "amount": (row.get("Amount") or "").strip(),
    }
