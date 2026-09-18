from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from itertools import combinations

from .forecast import (
    FORECAST_DAYS,
    UserState,
    amount_safe_to_pay,
    apply_spending_changes,
    earliest_full_payment,
    simulate,
)
from .fx import D, parse_date


def amount_decimals(s: str) -> int:
    if "." in str(s):
        return len(str(s).split(".")[1])
    return 0


def fmt_money(x: Decimal, decimals: int | None = None) -> str:
    if x is None:
        return ""
    if decimals is not None:
        q = x.quantize(Decimal("1").scaleb(-decimals), rounding=ROUND_HALF_UP)
        return format(q, f".{decimals}f")
    q = x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def fmt_plan(payments: list[tuple[date, Decimal]], option_amounts: list[str] | None = None) -> str:
    parts = []
    for i, (d, a) in enumerate(payments):
        if option_amounts and i < len(option_amounts):
            parts.append(f"{d.isoformat()}:{option_amounts[i]}")
        else:
            parts.append(f"{d.isoformat()}:{fmt_money(a)}")
    return "|".join(parts) if parts else "none"


def installment_dates(opt: dict) -> list[date]:
    n = int(opt["number_of_payments"])
    first = parse_date(opt["first_payment_date"])
    freq = opt.get("payment_frequency_days") or ""
    step = int(freq) if freq.strip() else 0
    dates = [first]
    for i in range(1, n):
        dates.append(first + timedelta(days=step * i))
    return dates


def option_eligible(opt: dict, profile: dict, consider: set[str], deadline: date) -> bool:
    method = opt["payment_method"]
    if method == "installments" and "installments" not in consider:
        return False
    if method == "full_payment":
        return False  # we synthesize full payment ourselves
    max_m = (profile.get("max_installment_months") or "").strip()
    if method == "installments":
        if not max_m:
            return False
        n = int(opt["number_of_payments"])
        if n > int(max_m):
            return False
        dates = installment_dates(opt)
        if dates[-1] > deadline:
            return False
        # every installment in the 90-day window? allow later ones if still <= deadline
        # but spec requires completing by deadline; 90-day check covers those in window
    return True


def format_changes(changes: list[tuple[str, str, Decimal | None]]) -> str:
    if not changes:
        return "none"
    parts = []
    for kind, eid, amt in changes:
        if kind == "stop":
            parts.append(f"stop:{eid}")
        else:
            parts.append(f"reduce_to:{eid}:{fmt_money(amt)}")
    return "|".join(parts)


@dataclass
class Candidate:
    completes: bool
    no_changes: bool
    total_paid: Decimal
    start: date
    n_payments: int
    option_id: str
    method: str
    status: str
    plan: list[tuple[date, Decimal]]
    plan_str: str
    changes: list
    trough: Decimal
    extra: dict


def _rank_key(c: Candidate):
    # lower is better; option ids like payment_option_05
    oid = 10**9
    if c.option_id.startswith("payment_option_"):
        try:
            oid = int(c.option_id.split("_")[-1])
        except Exception:
            oid = 10**9
    return (
        0 if c.completes else 1,
        0 if c.no_changes else 1,
        c.total_paid,
        c.start,
        c.n_payments,
        oid,
    )


def flexible_actions(state: UserState) -> list[tuple[str, str, Decimal | None]]:
    """Possible single actions targeting latest event of a series."""
    actions = []
    seen = set()
    for sid, meta in state.series.items():
        cat = meta["category"]
        if cat in state.protected:
            continue
        flex = meta["flexibility"]
        eid = meta["source_event_id"]
        if eid in seen:
            continue
        seen.add(eid)
        if cat in state.stop_cats and flex in ("stoppable", "reducible_or_stoppable"):
            actions.append(("stop", eid, None))
        if cat in state.reduce_cats and flex in ("reducible", "reducible_or_stoppable"):
            cur = meta["amount"]
            mn = meta["minimum_allowed"]
            if mn is None:
                mn = Decimal("0")
            if cur > mn:
                actions.append(("reduce", eid, mn))  # placeholder; amount searched later
    return actions


def _safe_with(state: UserState, request_date: date, extras: list[tuple[date, Decimal]], changes) -> tuple[bool, Decimal]:
    flows = apply_spending_changes(state, changes) if changes else state.flows
    return simulate(state.start_balance, state.min_keep, request_date, flows, extras)


def _reduce_search(state, request_date, extras, base_changes, eid, cur_amt, min_amt) -> Decimal | None:
    """Smallest cut (largest remaining amount) that makes extras safe."""
    # if min not enough, fail
    trial = base_changes + [("reduce", eid, min_amt)]
    ok, _ = _safe_with(state, request_date, extras, trial)
    if not ok:
        return None
    lo = int((min_amt * 100).to_integral_value())
    hi = int((cur_amt * 100).to_integral_value())
    best = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        amt = Decimal(mid) / Decimal(100)
        trial = base_changes + [("reduce", eid, amt)]
        ok, _ = _safe_with(state, request_date, extras, trial)
        if ok:
            best = mid
            lo = mid + 1  # try smaller cut (higher amount)
        else:
            hi = mid - 1
    return Decimal(best) / Decimal(100)


def enumerate_change_sets(state: UserState, request_date: date, extras: list[tuple[date, Decimal]], max_n=3):
    """Yield change lists that make extras safe, including empty."""
    yield []
    actions = flexible_actions(state)
    if not actions:
        return
    # try combinations of up to 3 series, not both stop and reduce same event
    stops = [a for a in actions if a[0] == "stop"]
    reduces = [a for a in actions if a[0] == "reduce"]
    singles = stops + reduces
    combos = []
    for k in range(1, min(max_n, len(singles)) + 1):
        for combo in combinations(singles, k):
            eids = [c[1] for c in combo]
            if len(set(eids)) != len(eids):
                continue
            combos.append(combo)
    # prefer fewer changes: already ordered by k
    for combo in combos:
        built = []
        ok_build = True
        for kind, eid, mn in combo:
            if kind == "stop":
                built.append(("stop", eid, None))
            else:
                meta = None
                for sid, m in state.series.items():
                    if m["source_event_id"] == eid:
                        meta = m
                        break
                if not meta:
                    ok_build = False
                    break
                found = _reduce_search(state, request_date, extras, built, eid, meta["amount"], mn or Decimal("0"))
                if found is None:
                    ok_build = False
                    break
                built.append(("reduce", eid, found))
        if ok_build:
            ok, _ = _safe_with(state, request_date, extras, built)
            if ok:
                yield built


def build_candidates(
    state: UserState,
    request: dict,
    profile: dict,
    options: list[dict],
) -> tuple[list[Candidate], Decimal, date | None]:
    request_date = parse_date(request["request_date"])
    deadline = parse_date(request["desired_completion_date"])
    requested = D(request["requested_amount"])
    consider = set((profile.get("payment_methods_user_will_consider") or "").split("|"))
    consider = {c.strip() for c in consider if c.strip()}
    allows_partial = str(request.get("allows_partial_payment", "")).lower() == "true"

    safe_today = amount_safe_to_pay(state, requested, request_date)
    # cap display: if extremely close to requested, treat as requested
    if requested - safe_today <= Decimal("0.009"):
        safe_today = requested
    earliest = earliest_full_payment(state, requested, request_date)

    cands: list[Candidate] = []

    decs = amount_decimals(request["requested_amount"])

    def add_cand(method, status, payments, changes, total, option_id="", option_amt_strs=None, extra=None):
        flows_ok, trough = _safe_with(state, request_date, payments, changes)
        if not flows_ok:
            return
        last_pay = max(p[0] for p in payments) if payments else request_date
        completes = bool(payments) and last_pay <= deadline
        if method in ("full_payment", "wait", "partial_payment"):
            paid = sum((p[1] for p in payments), Decimal("0"))
            completes = completes and paid + Decimal("0.05") >= requested
        if method == "installments":
            completes = bool(payments) and last_pay <= deadline
        amt_strs = option_amt_strs
        if amt_strs is None and method in ("full_payment", "wait") and decs:
            amt_strs = [fmt_money(p[1], decs) for p in payments]
        cands.append(
            Candidate(
                completes=completes,
                no_changes=not changes,
                total_paid=total,
                start=payments[0][0] if payments else date.max,
                n_payments=len(payments),
                option_id=option_id or "zzz",
                method=method,
                status=status,
                plan=payments,
                plan_str=fmt_plan(payments, amt_strs),
                changes=changes,
                trough=trough,
                extra=extra or {},
            )
        )

    # full payment (today) with optional spending changes
    if "full_payment" in consider:
        extras = [(request_date, requested)]
        for ch in enumerate_change_sets(state, request_date, extras):
            ok, _ = _safe_with(state, request_date, extras, ch)
            if not ok:
                continue
            status = "affordable_now" if not ch else "affordable_with_plan"
            add_cand("full_payment", status, extras, ch, requested)
            break  # first is empty or fewest changes

    # wait
    if "full_payment" in consider and earliest is not None and earliest > request_date and earliest <= deadline:
        extras = [(earliest, requested)]
        ok, _ = _safe_with(state, request_date, extras, [])
        if ok:
            add_cand("wait", "affordable_later", extras, [], requested)

    # partial
    if (
        allows_partial
        and "partial_payment" in consider
        and Decimal("0") < safe_today < requested
        and earliest is not None
        and earliest <= deadline
        and earliest != request_date
    ):
        rest = requested - safe_today
        extras = [(request_date, safe_today), (earliest, rest)]
        for ch in enumerate_change_sets(state, request_date, extras):
            if ch:
                continue
            add_cand("partial_payment", "affordable_with_plan", extras, [], requested)
            break

    # installments
    for opt in options:
        if opt["payment_method"] != "installments":
            continue
        if not option_eligible(opt, profile, consider, deadline):
            continue
        dates = installment_dates(opt)
        amt = D(opt["payment_amount"])
        extras = [(d, amt) for d in dates if d <= request_date + timedelta(days=FORECAST_DAYS - 1)]
        # if some dates outside window, still include those within deadline for ranking/completes
        extras_all = [(d, amt) for d in dates]
        # simulate only those in window (extras)
        total = D(opt["total_payable_amount"])
        amt_strs = [opt["payment_amount"]] * len(dates)
        for ch in enumerate_change_sets(state, request_date, extras):
            ok, _ = _safe_with(state, request_date, extras, ch)
            if not ok:
                continue
            status = "affordable_with_plan"
            add_cand(
                "installments",
                status,
                extras_all,
                ch,
                total,
                option_id=opt["payment_option_id"],
                option_amt_strs=amt_strs,
            )
            break

    return cands, safe_today, earliest


def choose(cands: list[Candidate], safe_today: Decimal, earliest: date | None, requested: Decimal, request_date: date, min_keep: Decimal) -> dict:
    completing = [c for c in cands if c.completes]
    pool = completing if completing else cands
    if not pool:
        return {
            "method": "not_recommended",
            "status": "not_affordable",
            "plan": "none",
            "changes": "none",
            "trough": None,
            "candidate": None,
        }
    best = sorted(pool, key=_rank_key)[0]
    status = best.status
    if best.method == "wait":
        status = "affordable_later"
    elif best.method == "full_payment" and best.no_changes and best.start == request_date:
        status = "affordable_now"
    elif best.method == "not_recommended":
        status = "not_affordable"
    else:
        if best.completes:
            if best.method == "full_payment" and best.no_changes:
                status = "affordable_now"
            else:
                status = "affordable_with_plan" if best.method != "wait" else "affordable_later"
        else:
            status = "not_affordable"
    return {
        "method": best.method,
        "status": status,
        "plan": best.plan_str,
        "changes": format_changes(best.changes),
        "trough": best.trough,
        "candidate": best,
    }
