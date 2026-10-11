"""Build the `co_retail` company database from `sample_supplies.sales`.

Copies the sample sales, shifts dates forward 8 years (2013-2017 becomes 2021-2025), adds a
unit cost to every item so margin can be computed, then plants six known stories. The answer
key goes to `bench/co_retail/planted.json` on disk, never into MongoDB.

Deterministic: the same seed always produces the same database (`content_hash` proves it).
"""

import argparse
import copy
import hashlib
import json
import random
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from bson import Decimal128, ObjectId, json_util
from pymongo import MongoClient

from common.settings import get_settings

SOURCE_DB, SOURCE_COLL = "sample_supplies", "sales"
COMPANY = "co_retail"
SEED = 20261011
YEAR_SHIFT = 8
BENCH = Path(__file__).resolve().parent.parent / "bench" / COMPANY

# Unit cost as a share of price. Printer paper and envelopes are the low margin products.
COST_RATIO = {
    "printer paper": Decimal("0.85"),
    "envelopes": Decimal("0.80"),
    "laptop": Decimal("0.55"),
    "backpack": Decimal("0.50"),
    "binder": Decimal("0.45"),
    "notepad": Decimal("0.40"),
    "pens": Decimal("0.35"),
}
LOW_MARGIN = ("printer paper", "envelopes")
TEST_EMAIL_DOMAIN = "test.example.com"

PLANTED = [
    {
        "id": "seattle_revenue_drop",
        "title": "Seattle revenue fell by about half in Q2 2024",
        "metric": "revenue",
        "segment": {"storeLocation": "Seattle"},
        "period": {"start": "2024-04-01", "end": "2024-06-30"},
        "direction": "down",
    },
    {
        "id": "online_satisfaction_drop",
        "title": "Online customer satisfaction fell from March 2025",
        "metric": "satisfaction",
        "segment": {"purchaseMethod": "Online"},
        "period": {"start": "2025-03-01", "end": "2025-12-31"},
        "direction": "down",
    },
    {
        "id": "london_coupon_spike",
        "title": "London coupon use spiked and order size shrank in Nov to Dec 2023",
        "metric": "coupon_rate",
        "segment": {"storeLocation": "London"},
        "period": {"start": "2023-11-01", "end": "2023-12-31"},
        "direction": "up",
    },
    {
        "id": "austin_laptop_surge",
        "title": "Austin laptop sales surged in H2 2025",
        "metric": "units",
        "segment": {"storeLocation": "Austin", "items.name": "laptop"},
        "period": {"start": "2025-07-01", "end": "2025-12-31"},
        "direction": "up",
    },
    {
        "id": "denver_test_accounts",
        "title": "Test accounts inflated Denver revenue in August 2024",
        "metric": "revenue",
        "segment": {"storeLocation": "Denver"},
        "period": {"start": "2024-08-01", "end": "2024-08-31"},
        "direction": "anomaly",
    },
    {
        "id": "low_margin_mix_shift",
        "title": "Low margin products (printer paper, envelopes) grew as a share of sales through 2025",
        "metric": "margin",
        "segment": {"items.name": list(LOW_MARGIN)},
        "period": {"start": "2025-01-01", "end": "2025-12-31"},
        "direction": "down",
    },
]


def d128(value: Decimal) -> Decimal128:
    return Decimal128(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def in_range(dt: datetime, start: str, end: str) -> bool:
    return datetime.fromisoformat(start) <= dt < datetime.fromisoformat(end) + timedelta(days=1)


def base_docs(client: MongoClient) -> list[dict]:
    docs = list(client[SOURCE_DB][SOURCE_COLL].find().sort("_id", 1))
    for doc in docs:
        doc["saleDate"] = doc["saleDate"].replace(year=doc["saleDate"].year + YEAR_SHIFT)
        for item in doc["items"]:
            item["cost"] = d128(item["price"].to_decimal() * COST_RATIO[item["name"]])
    return docs


def plant(docs: list[dict], rng: random.Random) -> list[dict]:
    p = {s["id"]: s["period"] for s in PLANTED}
    laptop_template = next(i for d in docs for i in d["items"] if i["name"] == "laptop")
    out = []
    for doc in docs:
        dt, store = doc["saleDate"], doc["storeLocation"]

        # 1. Seattle revenue drop: remove 45% of Seattle sales in Q2 2024.
        if store == "Seattle" and in_range(dt, **p["seattle_revenue_drop"]) and rng.random() < 0.45:
            continue

        # 2. Online satisfaction drop from March 2025.
        if doc["purchaseMethod"] == "Online" and in_range(dt, **p["online_satisfaction_drop"]):
            if rng.random() < 0.7:
                doc["customer"]["satisfaction"] = max(1, doc["customer"]["satisfaction"] - 2)

        # 3. London coupon spike with smaller orders, Nov to Dec 2023.
        if store == "London" and in_range(dt, **p["london_coupon_spike"]) and rng.random() < 0.8:
            doc["couponUsed"] = True
            for item in doc["items"]:
                item["quantity"] = max(1, item["quantity"] // 2)

        # 4. Austin laptop surge in H2 2025.
        if store == "Austin" and in_range(dt, **p["austin_laptop_surge"]) and rng.random() < 0.6:
            laptop = copy.deepcopy(laptop_template)
            laptop["quantity"] = rng.randint(2, 4)
            doc["items"].append(laptop)

        # 6. Low margin mix shift: printer paper and envelope volume ramps up through 2025.
        if dt.year == 2025:
            factor = 1 + 3.0 * (dt.month / 12)
            for item in doc["items"]:
                if item["name"] in LOW_MARGIN:
                    item["quantity"] = max(1, round(item["quantity"] * factor))

        out.append(doc)

    # 5. Test accounts: 40 fake Denver sales in August 2024.
    denver_aug = [d for d in out if d["storeLocation"] == "Denver" and d["saleDate"].year == 2024]
    for n in range(40):
        fake = copy.deepcopy(rng.choice(denver_aug))
        fake.pop("_id")
        fake["saleDate"] = datetime(2024, 8, 1) + timedelta(minutes=rng.randint(0, 31 * 24 * 60 - 1))
        fake["customer"]["email"] = f"qa+{n:03d}@{TEST_EMAIL_DOMAIN}"
        # Stable _id so rebuilds are identical.
        fake["_id"] = ObjectId(hashlib.sha1(f"{SEED}-test-{n}".encode()).hexdigest()[:24])
        out.append(fake)
    return out


def content_hash(docs: list[dict]) -> str:
    rows = sorted(json_util.dumps(d, sort_keys=True) for d in docs)
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def build(write: bool = True) -> str:
    client = MongoClient(get_settings().mongodb_uri)
    docs = plant(base_docs(client), random.Random(SEED))
    digest = content_hash(docs)
    if write:
        coll = client[COMPANY]["sales"]
        coll.drop()
        coll.insert_many(docs)
        coll.create_index("saleDate")
        coll.create_index("storeLocation")
        BENCH.mkdir(parents=True, exist_ok=True)
        key = {"company": COMPANY, "seed": SEED, "content_hash": digest, "findings": PLANTED}
        (BENCH / "planted.json").write_text(json.dumps(key, indent=2) + "\n")
        print(f"Wrote {len(docs)} sales to {COMPANY}.sales (hash {digest[:12]})")
    return digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Compute the hash without writing")
    args = parser.parse_args()
    build(write=not args.dry_run)
