#!/usr/bin/env python3
from __future__ import annotations

import csv
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from agent.evidence import build_event_amount_map, parse_messages
from agent.forecast import build_user_state
from agent.fx import FxTable, parse_date, D
from agent.loaders import load_all, read_csv
from agent.planner import build_candidates, choose, fmt_money
from main import solve_request, OUT_COLUMNS


def money_close(a: str, b: str, tol=Decimal("1")) -> bool:
    if a == b:
        return True
    try:
        da, db = D(a), D(b)
    except Exception:
        return False
    if da == 0 and db == 0:
        return True
    return abs(da - db) <= max(tol, abs(db) * Decimal("0.005"))


def main():
    data = load_all()
    fx = FxTable(data["fx_rows"])
    blanks = build_event_amount_map(data["images"], data["dataset_dir"])
    samples = read_csv(data["dataset_dir"] / "sample_requests.csv")
    # samples are labeled requests; they may not be in requests.csv
    scores = {k: 0 for k in OUT_COLUMNS if k != "request_id"}
    n = 0
    for s in samples:
        req = {k: s[k] for k in [
            "request_id", "user_id", "request_date", "request_type",
            "requested_amount", "desired_completion_date", "allows_partial_payment", "request_text",
        ]}
        pred = solve_request(data, fx, blanks, req)
        n += 1
        print("=" * 60)
        print(s["request_id"])
        for col in OUT_COLUMNS[1:]:
            ok = pred[col] == s[col]
            if col in ("amount_safe_to_pay",) and money_close(pred[col], s[col]):
                ok = True
            if ok:
                scores[col] += 1
            mark = "OK" if ok else "DIFF"
            if not ok:
                print(f"  {mark} {col}")
                print(f"    gold: {s[col]}")
                print(f"    pred: {pred[col]}")
        print(
            f"  pred method={pred['recommended_payment_method']} status={pred['affordability_status']} "
            f"safe={pred['amount_safe_to_pay']} earliest={pred['earliest_date_for_full_payment']} ch={pred['spending_changes_needed']}"
        )
    print("\nSCORE")
    for col, v in scores.items():
        print(f"  {col}: {v}/{n}")


if __name__ == "__main__":
    main()
