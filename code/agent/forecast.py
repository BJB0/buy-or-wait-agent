from __future__ import annotations

import calendar
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from .fx import D, FxTable, parse_date

FORECAST_DAYS = 90

IGNORE_STATUS = {"failed", "cancelled", "unrealized"}
ONE_OFF_INCOME_HINTS = (
    "bonus",
    "commission",
    "arrears",
    "prize",
    "lottery",
    "windfall",
    "reimbursement",
    "refund",
)
STOP_INCOME_HINTS = ("final employer",)


def add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def add_interval(d: date, kind: str) -> date:
    if kind == "monthly":
        return add_months(d, 1)
    if kind == "biweekly":
        return d + timedelta(days=14)
    if kind == "triweekly":
        return d + timedelta(days=21)
    if kind == "weekly":
        return d + timedelta(days=7)
    return d + timedelta(days=30)


def classify_interval(diffs: list[int]) -> str | None:
    if not diffs:
        return None
    med = statistics.median(diffs)
    if 26 <= med <= 35:
        return "monthly"
    if 12 <= med <= 16:
        return "biweekly"
    if 5 <= med <= 9:
        return "weekly"
    return None


@dataclass
class Flow:
    on: date
    amount: Decimal  # signed: credit+, debit-
    series_id: str
    source_event_id: str
    category: str
    flexibility: str
    minimum_allowed: Decimal | None
    description: str
    is_projection: bool


@dataclass
class UserState:
    start_balance: Decimal
    min_keep: Decimal
    home: str
    flows: list[Flow]
    series: dict[str, dict]
    protected: set[str]
    reduce_cats: set[str]
    stop_cats: set[str]


def _split_pipe(s: str) -> set[str]:
    if not s or not str(s).strip():
        return set()
    return {p.strip() for p in s.split("|") if p.strip()}


def _event_amount(e: dict, blanks: dict[str, Decimal]) -> Decimal | None:
    raw = (e.get("amount") or "").strip()
    if raw:
        return D(raw)
    return blanks.get(e["event_id"])


def _is_pending_credit(e: dict) -> bool:
    return e.get("status") == "pending" and e.get("direction") == "credit"


def _skip_event(e: dict, linked_settled: set[str]) -> bool:
    st = e.get("status") or ""
    if st in IGNORE_STATUS:
        return True
    if st == "unrealized" or e.get("direction") == "non_cash":
        return True
    if _is_pending_credit(e):
        return True
    # pending duplicate of an already-settled/pending original
    link = (e.get("linked_event_id") or "").strip()
    if st == "pending" and link and "duplicate" in (e.get("description") or "").lower():
        return True
    return False


def _income_is_recurring(e: dict, patch: dict) -> bool:
    desc = (e.get("description") or "").lower()
    cat = e.get("category") or ""
    if cat == "windfall":
        return False
    if any(h in desc for h in ONE_OFF_INCOME_HINTS):
        return False
    if any(h in desc for h in STOP_INCOME_HINTS):
        return False
    if patch.get("ignore_commission") and "commission" in desc:
        return False
    if patch.get("ignore_bonus") and "bonus" in desc:
        return False
    return True


def build_user_state(
    profile: dict,
    events: list[dict],
    request_date: date,
    fx: FxTable,
    blank_amounts: dict[str, Decimal],
    message_patch: dict,
) -> UserState:
    home = profile["home_currency"]
    start = D(profile["current_available_balance"])
    min_keep = D(profile["minimum_balance_to_keep"])
    protected = _split_pipe(profile.get("expense_categories_to_protect", ""))
    reduce_cats = _split_pipe(profile.get("expense_categories_user_is_willing_to_reduce", ""))
    stop_cats = _split_pipe(profile.get("expense_categories_user_is_willing_to_stop", ""))

    horizon_end = request_date + timedelta(days=FORECAST_DAYS - 1)

    usable = []
    for e in events:
        if _skip_event(e, set()):
            continue
        amt = _event_amount(e, blank_amounts)
        if amt is None:
            continue
        settle_s = (e.get("settlement_date") or e.get("event_date") or "").strip()
        if not settle_s:
            continue
        settle = parse_date(settle_s)
        ccy = e.get("currency") or home
        home_amt = fx.convert(amt, ccy, home, settle)
        usable.append({**e, "_amt": home_amt, "_settle": settle, "_orig": amt})

    # Explicit future cash: pending debits and scheduled items on/after request_date
    flows: list[Flow] = []
    counted_ids = set()
    for e in usable:
        st = e["status"]
        settle = e["_settle"]
        if settle < request_date:
            continue
        if st not in ("pending", "scheduled", "settled"):
            continue
        # settled in the future (unusual) still counts; settled in the past already in balance
        if st == "settled" and settle >= request_date:
            # already reflected if settlement is today or past? available_balance is as of request_date.
            # If settled on request_date, typically already in the snapshot. Skip settled <= request_date.
            if settle <= request_date:
                continue
        if st == "settled":
            continue
        sign = Decimal("1") if e["direction"] == "credit" else Decimal("-1")
        if e["direction"] == "credit" and st == "pending":
            continue
        flows.append(
            Flow(
                on=settle,
                amount=sign * e["_amt"],
                series_id=e["event_id"],
                source_event_id=e["event_id"],
                category=e.get("category") or "",
                flexibility=e.get("flexibility") or "fixed",
                minimum_allowed=D(e["minimum_allowed_amount"]) if e.get("minimum_allowed_amount") else None,
                description=e.get("description") or "",
                is_projection=False,
            )
        )
        counted_ids.add(e["event_id"])

    hist = [e for e in usable if e["status"] == "settled" and e["_settle"] < request_date]
    series_meta: dict[str, dict] = {}

    STRUCTURED_CATS = {
        "rent",
        "housing",
        "utilities",
        "insurance",
        "education",
        "family_support",
        "debt_repayment",
        "streaming",
        "cloud_storage",
        "music_subscription",
        "delivery_membership",
        "gym",
        "healthcare",
    }
    VARIABLE_CATS = {"groceries", "transport", "dining"}
    MONTHLY_OPTIONAL = {"shopping", "entertainment", "work_expense"}

    def _min_allowed(e0):
        return D(e0["minimum_allowed_amount"]) if e0.get("minimum_allowed_amount") else None

    def project_series(sid, e0, kind, amount, last, sign, members):
        series_meta[sid] = {
            "kind": kind,
            "amount": amount,
            "last": last,
            "category": e0.get("category") or "",
            "flexibility": e0.get("flexibility") or "fixed",
            "minimum_allowed": _min_allowed(e0),
            "description": e0.get("description") or "",
            "event_type": e0.get("event_type") or "",
            "direction": e0.get("direction") or "debit",
            "source_event_id": members[-1]["event_id"],
            "members": [m["event_id"] for m in members],
        }
        cur = add_interval(last, kind)
        if e0.get("category") == "salary" and message_patch.get("salary_date"):
            sd = message_patch["salary_date"]
            if sd >= request_date:
                cur = sd
        proj_amt = amount
        if message_patch.get("rent_increase_pct") is not None and e0.get("category") == "rent":
            proj_amt = (amount * (Decimal("1") + message_patch["rent_increase_pct"] / Decimal("100"))).quantize(
                Decimal("0.01")
            )
        while cur <= horizon_end:
            clash = any(
                abs((f.on - cur).days) <= 3 and f.category == (e0.get("category") or "") and not f.is_projection
                for f in flows
            )
            if not clash and cur >= request_date:
                flows.append(
                    Flow(
                        on=cur,
                        amount=sign * proj_amt,
                        series_id=sid,
                        source_event_id=members[-1]["event_id"],
                        category=e0.get("category") or "",
                        flexibility=e0.get("flexibility") or "fixed",
                        minimum_allowed=_min_allowed(e0),
                        description=e0.get("description") or "",
                        is_projection=True,
                    )
                )
            cur = add_interval(cur, kind)

    # --- salary as a single category series ---
    salaries = [
        e
        for e in usable
        if e.get("category") == "salary" and e["status"] in ("settled", "scheduled") and _income_is_recurring(e, message_patch)
    ]
    salaries = sorted(salaries, key=lambda x: x["_settle"])
    last_any_salary = sorted(
        [e for e in usable if e.get("category") == "salary"],
        key=lambda x: x["_settle"],
    )
    income_stopped = bool(message_patch.get("stop_income") or message_patch.get("gig_unconfirmed"))
    if last_any_salary:
        last_desc = (last_any_salary[-1].get("description") or "").lower()
        if "final" in last_desc:
            income_stopped = True
    rec_by_desc: dict[str, list] = defaultdict(list)
    for s in salaries:
        rec_by_desc[s.get("description") or "salary"].append(s)
    future_sal = [s for s in salaries if s["_settle"] >= request_date]
    if not income_stopped:
        if future_sal:
            # Use confirmed future salary rows as the ongoing cadence.
            seen_dom = set()
            for last_s in sorted(future_sal, key=lambda x: x["_settle"]):
                dom = last_s["_settle"].day
                if dom in seen_dom:
                    continue
                seen_dom.add(dom)
                amt = last_s["_amt"]
                sa = message_patch.get("salary_amount")
                if sa is not None and sa > Decimal("50"):
                    if not (Decimal("2018") <= sa <= Decimal("2035") and sa == sa.to_integral_value()):
                        amt = sa
                project_series(
                    f"salary:{last_s.get('description')}",
                    last_s,
                    "monthly",
                    amt,
                    last_s["_settle"],
                    Decimal("1"),
                    [last_s],
                )
        elif rec_by_desc:
            for desc, rec in rec_by_desc.items():
                rec = sorted(rec, key=lambda x: x["_settle"])
                last_s = rec[-1]
                if last_s["_settle"] < request_date and (request_date - last_s["_settle"]).days > 40:
                    continue
                amt = last_s["_amt"]
                sa = message_patch.get("salary_amount")
                if sa is not None and sa > Decimal("50"):
                    if not (Decimal("2018") <= sa <= Decimal("2035") and sa == sa.to_integral_value()):
                        amt = sa
                project_series(f"salary:{desc}", last_s, "monthly", amt, last_s["_settle"], Decimal("1"), rec)

    # --- structured monthly description series ---
    by_desc: dict[tuple[str, str], list] = defaultdict(list)
    for e in hist:
        cat = e.get("category") or ""
        et = e.get("event_type") or ""
        if cat == "salary":
            continue
        if et in ("refund", "investment_purchase", "investment_sale", "investment_valuation"):
            continue
        structured = (
            et in ("subscription", "debt_payment")
            or cat in STRUCTURED_CATS
            or (cat in MONTHLY_OPTIONAL)
        )
        if not structured:
            continue
        if cat in VARIABLE_CATS:
            continue
        key = (e.get("description") or "", cat)
        by_desc[key].append(e)

    for key, members in by_desc.items():
        members = sorted(members, key=lambda x: x["_settle"])
        if len(members) < 3:
            continue
        dates = [m["_settle"] for m in members]
        diffs = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        kind = classify_interval(diffs)
        if kind != "monthly":
            continue
        last = dates[-1]
        # stale series
        if (request_date - last).days > 40:
            continue
        e0 = members[-1]
        project_series(f"desc:{key[0]}|{key[1]}", e0, "monthly", e0["_amt"], last, Decimal("-1"), members)

    lookback = request_date - timedelta(days=90)
    for cat in sorted(VARIABLE_CATS | {"shopping", "entertainment"}):
        members = [
            e
            for e in hist
            if (e.get("category") or "") == cat
            and e["direction"] == "debit"
            and e.get("event_type") not in ("subscription", "debt_payment")
            and e["_settle"] >= lookback
        ]
        if len(members) < 3:
            continue
        if cat in MONTHLY_OPTIONAL and any(
            meta["category"] == cat and meta["kind"] == "monthly" for meta in series_meta.values()
        ):
            continue
        members = sorted(members, key=lambda x: x["_settle"])
        span_days = max(1, (request_date - max(lookback, members[0]["_settle"])).days)
        total = sum((m["_amt"] for m in members), Decimal("0"))
        weekly = (total / Decimal(span_days)) * Decimal("7") * Decimal("0.80")
        last = members[-1]["_settle"]
        flex_members = [
            m
            for m in members
            if (m.get("flexibility") or "fixed") in ("reducible", "stoppable", "reducible_or_stoppable")
        ]
        e0 = flex_members[-1] if flex_members else members[-1]
        sid = f"cat:{cat}"
        series_meta[sid] = {
            "kind": "weekly",
            "amount": weekly,
            "last": last,
            "category": cat,
            "flexibility": e0.get("flexibility") or "fixed",
            "minimum_allowed": _min_allowed(e0),
            "description": e0.get("description") or cat,
            "event_type": "expense",
            "direction": "debit",
            "source_event_id": e0["event_id"],
            "members": [m["event_id"] for m in members],
        }
        cur = last + timedelta(days=7)
        while cur <= horizon_end:
            if cur >= request_date:
                flows.append(
                    Flow(
                        on=cur,
                        amount=-weekly,
                        series_id=sid,
                        source_event_id=e0["event_id"],
                        category=cat,
                        flexibility=e0.get("flexibility") or "fixed",
                        minimum_allowed=_min_allowed(e0),
                        description=e0.get("description") or cat,
                        is_projection=True,
                    )
                )
            cur += timedelta(days=7)

    return UserState(
        start_balance=start,
        min_keep=min_keep,
        home=home,
        flows=flows,
        series=series_meta,
        protected=protected,
        reduce_cats=reduce_cats,
        stop_cats=stop_cats,
    )


def apply_spending_changes(state: UserState, changes: list[tuple[str, str, Decimal | None]]) -> list[Flow]:
    """changes: list of ('stop', event_id, None) or ('reduce', event_id, new_amount)."""
    stop_series = set()
    reduce_map: dict[str, Decimal] = {}
    # map source event id -> series_id
    ev_to_series = {}
    for sid, meta in state.series.items():
        ev_to_series[meta["source_event_id"]] = sid
        for mid in meta.get("members", []):
            ev_to_series[mid] = sid
    for kind, eid, new_amt in changes:
        sid = ev_to_series.get(eid)
        if not sid:
            # maybe the event_id is itself a one-off scheduled - ignore
            continue
        if kind == "stop":
            stop_series.add(sid)
        elif kind == "reduce" and new_amt is not None:
            reduce_map[sid] = new_amt
    out = []
    for f in state.flows:
        if f.series_id in stop_series:
            continue
        if f.series_id in reduce_map and f.amount < 0:
            out.append(
                Flow(
                    on=f.on,
                    amount=-abs(reduce_map[f.series_id]),
                    series_id=f.series_id,
                    source_event_id=f.source_event_id,
                    category=f.category,
                    flexibility=f.flexibility,
                    minimum_allowed=f.minimum_allowed,
                    description=f.description,
                    is_projection=f.is_projection,
                )
            )
        else:
            out.append(f)
    return out


def simulate(
    start: Decimal,
    min_keep: Decimal,
    request_date: date,
    flows: Iterable[Flow],
    extra_debits: list[tuple[date, Decimal]] | None = None,
) -> tuple[bool, Decimal]:
    by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    for f in flows:
        if request_date <= f.on <= request_date + timedelta(days=FORECAST_DAYS - 1):
            by_day[f.on] += f.amount
    if extra_debits:
        for d, amt in extra_debits:
            by_day[d] -= amt
    bal = start
    trough = bal
    for i in range(FORECAST_DAYS):
        d = request_date + timedelta(days=i)
        bal += by_day[d]
        if bal < trough:
            trough = bal
        if bal < min_keep:
            return False, trough
    return True, trough


def amount_safe_to_pay(state: UserState, requested: Decimal, request_date: date) -> Decimal:
    lo = Decimal("0")
    hi = requested
    if not simulate(state.start_balance, state.min_keep, request_date, state.flows, [])[0]:
        return Decimal("0")
    # binary search in cents
    scale = Decimal("0.01")
    left = 0
    right = int((requested / scale).to_integral_value())
    best = 0
    while left <= right:
        mid = (left + right) // 2
        p = Decimal(mid) * scale
        ok, _ = simulate(state.start_balance, state.min_keep, request_date, state.flows, [(request_date, p)])
        if ok:
            best = mid
            left = mid + 1
        else:
            right = mid - 1
    return Decimal(best) * scale


def earliest_full_payment(state: UserState, requested: Decimal, request_date: date) -> date | None:
    end = request_date + timedelta(days=FORECAST_DAYS - 1)
    d = request_date
    while d <= end:
        ok, _ = simulate(state.start_balance, state.min_keep, request_date, state.flows, [(d, requested)])
        if ok:
            return d
        d += timedelta(days=1)
    return None
