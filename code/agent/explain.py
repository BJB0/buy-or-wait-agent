from __future__ import annotations

from datetime import date
from decimal import Decimal

from .planner import fmt_money


CCY_PREFIX = {
    "INR": "INR ",
    "IDR": "IDR ",
    "ZAR": "ZAR ",
    "EUR": "EUR ",
    "USD": "USD ",
}


def money(home: str, amount: Decimal) -> str:
    prefix = CCY_PREFIX.get(home, home + " ")
    s = fmt_money(amount)
    # add thousands separators for explanation style
    if "." in s:
        whole, frac = s.split(".", 1)
    else:
        whole, frac = s, ""
    sign = ""
    if whole.startswith("-"):
        sign = "-"
        whole = whole[1:]
    grouped = ""
    while len(whole) > 3:
        grouped = "," + whole[-3:] + grouped
        whole = whole[:-3]
    grouped = whole + grouped
    if frac:
        body = f"{sign}{grouped}.{frac}"
    else:
        body = f"{sign}{grouped}"
    return prefix + body


def explain(row: dict, home: str, requested: Decimal, min_keep: Decimal, deadline: date) -> str:
    method = row["method"]
    plan = row.get("candidate")
    trough = row.get("trough")
    safe = row.get("safe_today")
    if method == "full_payment" and row["status"] == "affordable_now":
        extra = f" This leaves at least {money(home, min_keep)} available over the next 90 days."
        return f"Pay {money(home, requested)} today.{extra}".strip()
    if method == "full_payment" and row["changes"] != "none":
        return (
            f"Adjust flexible spending, then pay {money(home, requested)} today. "
            f"This leaves at least {money(home, min_keep)} available."
        )
    if method == "wait" and plan and plan.plan:
        d = plan.plan[0][0]
        return (
            f"Pay {money(home, requested)} in full on {d.strftime('%-d %B %Y') if False else _long_date(d)}. "
            f"Paying earlier would take the balance below the {money(home, min_keep)} minimum."
        )
    if method == "installments" and plan:
        n = plan.n_payments
        amt = plan.plan[0][1] if plan.plan else Decimal("0")
        start = plan.start
        return (
            f"Use {n} installments of {money(home, amt)}, starting {_long_date(start)}. "
            f"This leaves at least {money(home, min_keep)} available."
        )
    if method == "partial_payment" and plan and len(plan.plan) == 2:
        a0, a1 = plan.plan[0][1], plan.plan[1][1]
        d1 = plan.plan[1][0]
        return (
            f"Pay {money(home, a0)} today and the remaining {money(home, a1)} on {_long_date(d1)}. "
            f"This completes the full request and keeps the {money(home, min_keep)} minimum protected."
        )
    if method == "not_recommended":
        if safe and safe > 0:
            return (
                f"Do not proceed with the {money(home, requested)} request. "
                f"Although {money(home, safe)} is available today, the full amount cannot be completed safely within 90 days."
            )
        return (
            f"Do not make this payment by {_long_date(deadline)}. "
            f"None of the available options keeps the {money(home, min_keep)} minimum protected."
        )
    return f"Recommendation: {method}."


def _long_date(d: date) -> str:
    return f"{d.day} {d.strftime('%B')} {d.year}"
