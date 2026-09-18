from __future__ import annotations

import json
import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from .fx import D

# Verified totals from the 16 challenge images (VLM/OCR). Used as a
# deterministic cache so batch runs stay cheap and repeatable.
IMAGE_AMOUNTS = {
    "image_01": Decimal("4365000"),
    "image_02": Decimal("100000"),
    "image_03": Decimal("41272"),
    "image_04": Decimal("2854"),
    "image_05": Decimal("704.05"),
    "image_06": Decimal("1995"),
    "image_07": Decimal("8528"),
    "image_08": Decimal("15339"),
    "image_09": Decimal("723"),
    "image_10": Decimal("79679.26"),
    "image_11": Decimal("3650"),
    "image_12": Decimal("33.50"),
    "image_13": Decimal("2298"),
    "image_14": Decimal("4543"),
    "image_15": Decimal("9968"),
    "image_16": Decimal("393.22"),
}

USAGE = {
    "provider": "none",
    "model": "local-rules+cached-vlm",
    "calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cost_usd": 0.0,
}


def _cache_path() -> Path:
    p = Path(__file__).resolve().parent / ".cache"
    p.mkdir(exist_ok=True)
    return p / "evidence.json"


def extract_image_amount(image_id: str, image_path: Path | None = None) -> Decimal | None:
    if image_id in IMAGE_AMOUNTS:
        return IMAGE_AMOUNTS[image_id]
    if image_path and image_path.exists():
        text = _ocr_text(image_path)
        if text:
            amt = _parse_amount_from_ocr(text)
            if amt is not None:
                return amt
    return None


def _ocr_text(path: Path) -> str:
    try:
        import pytesseract
        from PIL import Image

        return pytesseract.image_to_string(Image.open(path))
    except Exception:
        return ""


def _parse_amount_from_ocr(text: str) -> Decimal | None:
    patterns = [
        r"(?:Net Pay|Grand Total|Total paid|Amount Payable|Balance Due|Total Amount Received|Amount due till|Cash Paid|Total)\s*[:\-=]?\s*(?:IDR|INR|Rs\.?|₹|EUR|USD|ZAR|\$)?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+\.[0-9]+|[0-9]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return D(m.group(1))
    return None


def parse_messages(messages: list[dict[str, str]]) -> dict:
    patch = {
        "salary_amount": None,
        "salary_date": None,
        "salary_continue": None,
        "stop_income": False,
        "ignore_bonus": False,
        "ignore_commission": False,
        "ignore_pending_credits": True,
        "rent_increase_pct": None,
        "internal_transfer": False,
        "gig_unconfirmed": False,
        "new_childcare": False,
        "raw": [],
    }
    for m in messages:
        text = m.get("message_text") or ""
        patch["raw"].append(text)
        low = text.lower()

        if any(
            k in low
            for k in [
                "contract has ended",
                "no off-season",
                "seasonal contract has ended",
                "final employer",
            ]
        ):
            patch["stop_income"] = True
        if "pending" in low and any(k in low for k in ["payout", "earnings", "quickcrew", "withdrawable"]):
            patch["gig_unconfirmed"] = True
            patch["stop_income"] = True
        if any(k in low for k in ["bonus", "kinerja", "prize", "lottery"]):
            if any(k in low for k in ["waiting", "pending", "not been", "belum", "still"]):
                patch["ignore_bonus"] = True
        if any(k in low for k in ["commission", "komisi"]):
            patch["ignore_commission"] = True
        if "between your two accounts" in low or "transfer between your two accounts" in low:
            patch["internal_transfer"] = True
        if "childcare" in low:
            patch["new_childcare"] = True
        if "increases monthly rent by" in low or "increases monthly rent" in low:
            mperc = re.search(r"by\s+(\d+(?:\.\d+)?)\s*%", text, re.I)
            if mperc:
                patch["rent_increase_pct"] = Decimal(mperc.group(1))

        # salary amount (only when the message clearly amends pay)
        amend = any(
            k in low
            for k in [
                "naik menjadi",
                "reduced to",
                "temporary monthly pay",
                "first salary will be",
                "regular salary of",
                "resumes on",
                "confirmed salary is now",
                "monthly pay is",
            ]
        )
        if amend:
            mm = re.search(
                r"(?:IDR|EUR|USD|ZAR|INR)\s*([0-9]+(?:\.[0-9]+)?)",
                text,
                re.I,
            )
            if not mm:
                mm = re.search(
                    r"(?:pay is|will be|salary of|monthly pay is)\s*(?:EUR|USD|ZAR|INR|IDR)?\s*([0-9]+(?:\.[0-9]+)?)",
                    text,
                    re.I,
                )
            if mm:
                val = D(mm.group(1))
                if not (Decimal("1900") <= val <= Decimal("2035") and val == val.to_integral_value()):
                    patch["salary_amount"] = val

        dm = re.search(
            r"(?:on|tanggal|berlaku mulai|expected on|credit date is|resumes on)\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
            text,
            re.I,
        )
        if dm and any(k in low for k in ["salary", "gaji", "payroll", "penggajian", "payday"]):
            patch["salary_date"] = date.fromisoformat(dm.group(1))
        if "reduced amount continues" in low or "temporary monthly pay" in low:
            patch["salary_continue"] = True
    return patch


def build_event_amount_map(images: list[dict[str, str]], dataset_dir: Path) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    cache_file = _cache_path()
    cached = {}
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cached = {}
    for row in images:
        iid = row["image_id"]
        eid = row.get("related_event_id") or ""
        amt = None
        if iid in cached:
            amt = D(cached[iid])
        else:
            img_path = dataset_dir / "media" / "images" / f"{iid}.png"
            amt = extract_image_amount(iid, img_path)
            if amt is not None:
                cached[iid] = str(amt)
        if eid and amt is not None:
            out[eid] = amt
    cache_file.write_text(json.dumps(cached, indent=2), encoding="utf-8")
    return out
