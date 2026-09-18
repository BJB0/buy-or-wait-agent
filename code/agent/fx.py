from __future__ import annotations

from datetime import date
from decimal import Decimal


def parse_date(s: str) -> date:
    return date.fromisoformat(s.strip())


def D(x) -> Decimal:
    if x is None or x == "":
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x).replace(",", "").strip())


class FxTable:
    def __init__(self, rows: list[dict[str, str]]):
        self.rates: dict[tuple[str, str, str], Decimal] = {}
        for r in rows:
            key = (r["rate_date"], r["from_currency"], r["to_currency"])
            self.rates[key] = D(r["rate"])

    def convert(self, amount: Decimal, from_ccy: str, to_ccy: str, on: date) -> Decimal:
        if from_ccy == to_ccy:
            return amount
        ds = on.isoformat()
        direct = self.rates.get((ds, from_ccy, to_ccy))
        if direct is not None:
            return amount * direct
        inv = self.rates.get((ds, to_ccy, from_ccy))
        if inv is not None and inv != 0:
            return amount / inv
        # nearest earlier date with same pair
        candidates = [
            (k[0], v)
            for k, v in self.rates.items()
            if k[1] == from_ccy and k[2] == to_ccy and k[0] <= ds
        ]
        if candidates:
            candidates.sort()
            return amount * candidates[-1][1]
        inv_c = [
            (k[0], v)
            for k, v in self.rates.items()
            if k[1] == to_ccy and k[2] == from_ccy and k[0] <= ds
        ]
        if inv_c:
            inv_c.sort()
            rate = inv_c[-1][1]
            if rate != 0:
                return amount / rate
        return amount
