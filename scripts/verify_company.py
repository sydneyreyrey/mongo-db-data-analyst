"""Print the size of each planted story in `co_retail` so we know each one is findable."""

from datetime import datetime

from pymongo import MongoClient

from common.settings import get_settings

REVENUE = {
    "$sum": {
        "$map": {
            "input": "$items",
            "as": "i",
            "in": {"$multiply": [{"$toDouble": "$$i.price"}, "$$i.quantity"]},
        }
    }
}
MARGIN = {
    "$sum": {
        "$map": {
            "input": "$items",
            "as": "i",
            "in": {
                "$multiply": [
                    {"$subtract": [{"$toDouble": "$$i.price"}, {"$toDouble": "$$i.cost"}]},
                    "$$i.quantity",
                ]
            },
        }
    }
}


def by_month(coll, match: dict, value: dict, year_from: int, year_to: int) -> dict[str, float]:
    rows = coll.aggregate(
        [
            {
                "$match": {
                    **match,
                    "saleDate": {"$gte": datetime(year_from, 1, 1), "$lt": datetime(year_to + 1, 1, 1)},
                }
            },
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m", "date": "$saleDate"}}, "v": value}},
            {"$sort": {"_id": 1}},
        ]
    )
    return {r["_id"]: round(r["v"], 2) for r in rows}


def show(title: str, series: dict[str, float]) -> None:
    print(f"\n{title}")
    print("  " + "  ".join(f"{k[2:]}:{v:g}" for k, v in series.items()))


def main() -> None:
    sales = MongoClient(get_settings().mongodb_uri)["co_retail"]["sales"]
    show(
        "1. Seattle revenue by month (drop Apr to Jun 2024)",
        by_month(sales, {"storeLocation": "Seattle"}, {"$sum": REVENUE}, 2024, 2024),
    )
    show(
        "2. Online avg satisfaction by month (drop from Mar 2025)",
        by_month(sales, {"purchaseMethod": "Online"}, {"$avg": "$customer.satisfaction"}, 2024, 2025),
    )
    show(
        "3. London coupon rate by month (spike Nov to Dec 2023)",
        by_month(sales, {"storeLocation": "London"}, {"$avg": {"$cond": ["$couponUsed", 1, 0]}}, 2023, 2023),
    )
    show(
        "4. Austin laptop units by month (surge Jul to Dec 2025)",
        by_month(
            sales,
            {"storeLocation": "Austin"},
            {
                "$sum": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {"input": "$items", "cond": {"$eq": ["$$this.name", "laptop"]}}
                            },
                            "in": "$$this.quantity",
                        }
                    }
                }
            },
            2025,
            2025,
        ),
    )
    show(
        "5. Denver revenue by month (spike Aug 2024)",
        by_month(sales, {"storeLocation": "Denver"}, {"$sum": REVENUE}, 2024, 2024),
    )
    rev = by_month(sales, {}, {"$sum": REVENUE}, 2024, 2025)
    mar = by_month(sales, {}, {"$sum": MARGIN}, 2024, 2025)
    show(
        "6. Margin rate % by month, all stores (declines through 2025)",
        {k: round(100 * mar[k] / rev[k], 1) for k in rev},
    )
    print(
        "\nTest account sales:",
        sales.count_documents({"customer.email": {"$regex": "@test\\.example\\.com$"}}),
    )


if __name__ == "__main__":
    main()
