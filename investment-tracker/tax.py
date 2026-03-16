from datetime import date, datetime
from dateutil.relativedelta import relativedelta


# Czech tax time test: 3 years holding period for tax exemption
# § 4 odst. 1 písm. w) zákona č. 586/1992 Sb. (Income Tax Act)
TAX_FREE_YEARS = 3


def get_tax_status(buy_date):
    """Calculate Czech time-test status for a given buy date.

    Args:
        buy_date: date or ISO string (YYYY-MM-DD)

    Returns dict with:
        days_held       – days since purchase
        tax_free_date   – exact date when position becomes tax-free
        days_remaining  – days until tax-free (0 if already tax-free)
        is_tax_free     – bool
        badge_class     – Bootstrap badge color class
        badge_label     – human-readable status label
    """
    if isinstance(buy_date, str):
        buy_date = datetime.strptime(buy_date, "%Y-%m-%d").date()

    today = date.today()
    tax_free_date = buy_date + relativedelta(years=TAX_FREE_YEARS)

    days_held = (today - buy_date).days
    is_tax_free = today >= tax_free_date
    days_remaining = max(0, (tax_free_date - today).days)

    if is_tax_free:
        badge_class = "success"
        badge_label = "Tax-free"
    elif days_remaining <= 180:
        badge_class = "warning"
        badge_label = f"{days_remaining}d remaining"
    else:
        badge_class = "danger"
        badge_label = f"{days_remaining}d remaining"

    return {
        "days_held": days_held,
        "tax_free_date": tax_free_date,
        "days_remaining": days_remaining,
        "is_tax_free": is_tax_free,
        "badge_class": badge_class,
        "badge_label": badge_label,
    }


def enrich_lots_with_tax(lots):
    """Add tax status fields to a list of open lot dicts."""
    for lot in lots:
        status = get_tax_status(lot["date"])
        lot.update(status)
    return lots
