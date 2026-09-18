#!/usr/bin/env python3
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from agent.evidence import build_event_amount_map, parse_messages
from agent.explain import explain
from agent.forecast import build_user_state
from agent.fx import FxTable, parse_date, D
from agent.loaders import load_all
from agent.planner import build_candidates, choose, fmt_money


OUT_COLUMNS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]


def solve_request(data, fx, blanks, request) -> dict:
    uid = request["user_id"]
    profile = data["profiles"][uid]
    events = data["events_by_user"].get(uid, [])
    msgs = data["messages"].get(uid, [])
    patch = parse_messages(msgs)
    rd = parse_date(request["request_date"])
    state = build_user_state(profile, events, rd, fx, blanks, patch)
    options = data["options"].get(request["request_id"], [])
    cands, safe, earliest = build_candidates(state, request, profile, options)
    chosen = choose(cands, safe, earliest, D(request["requested_amount"]), rd, state.min_keep)
    chosen["safe_today"] = safe
    home = profile["home_currency"]
    text = explain(
        chosen,
        home,
        D(request["requested_amount"]),
        state.min_keep,
        parse_date(request["desired_completion_date"]),
    )
    # refine full_payment-with-plan explanations using change labels
    if chosen["method"] == "full_payment" and chosen["changes"] != "none" and chosen.get("candidate"):
        text = _changes_full_expl(chosen, home, D(request["requested_amount"]), data)
    earliest_s = earliest.isoformat() if earliest else ""
    if chosen["status"] == "affordable_now":
        earliest_s = request["request_date"]
    return {
        "request_id": request["request_id"],
        "amount_safe_to_pay": fmt_money(safe),
        "affordability_status": chosen["status"],
        "recommended_payment_method": chosen["method"],
        "payment_plan": chosen["plan"],
        "earliest_date_for_full_payment": earliest_s,
        "spending_changes_needed": chosen["changes"],
        "decision_explanation": text,
    }


def _changes_full_expl(chosen, home, requested, data):
    from agent.explain import money

    parts = []
    events_by_id = data["events_by_id"]
    cand = chosen["candidate"]
    for kind, eid, amt in cand.changes:
        e = events_by_id.get(eid, {})
        desc = (e.get("description") or "flexible expense").lower()
        if kind == "stop":
            parts.append(f"Stop the {desc}")
        else:
            parts.append(f"Reduce the {desc} to {money(home, amt)}")
    lead = ", ".join(parts)
    if len(parts) > 1:
        lead = ", ".join(parts[:-1]) + " and " + parts[-1]
    trough = chosen.get("trough")
    tail = f"This leaves at least {money(home, trough)} available." if trough is not None else ""
    return f"{lead}, then pay {money(home, requested)} today. {tail}".strip()


def main():
    data = load_all()
    fx = FxTable(data["fx_rows"])
    blanks = build_event_amount_map(data["images"], data["dataset_dir"])
    rows = []
    for req in data["requests"]:
        rows.append(solve_request(data, fx, blanks, req))
        if len(rows) % 25 == 0:
            print(f"processed {len(rows)}/{len(data['requests'])}", flush=True)
    out = ROOT / "output.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
