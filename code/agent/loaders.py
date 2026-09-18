from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def dataset_dir() -> Path:
    return repo_root() / "dataset"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_all(base: Path | None = None) -> dict[str, Any]:
    d = base or dataset_dir()
    profiles = {r["user_id"]: r for r in read_csv(d / "financial_profiles.csv")}
    events = read_csv(d / "financial_events.csv")
    events_by_user: dict[str, list[dict[str, str]]] = {}
    events_by_id: dict[str, dict[str, str]] = {}
    for e in events:
        events_by_user.setdefault(e["user_id"], []).append(e)
        events_by_id[e["event_id"]] = e
    options: dict[str, list[dict[str, str]]] = {}
    for o in read_csv(d / "request_payment_options.csv"):
        options.setdefault(o["request_id"], []).append(o)
    messages: dict[str, list[dict[str, str]]] = {}
    for m in read_csv(d / "messages.csv"):
        messages.setdefault(m["user_id"], []).append(m)
    images = read_csv(d / "images.csv")
    fx_rows = read_csv(d / "exchange_rates.csv")
    requests = read_csv(d / "requests.csv")
    return {
        "profiles": profiles,
        "events": events,
        "events_by_user": events_by_user,
        "events_by_id": events_by_id,
        "options": options,
        "messages": messages,
        "images": images,
        "fx_rows": fx_rows,
        "requests": requests,
        "dataset_dir": d,
    }
