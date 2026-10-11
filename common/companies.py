"""Company databases the demo knows how to connect to.

This is connection metadata (which field dates each record), the kind of thing an admin supplies when
connecting a database. It is not learned knowledge and holds nothing about what matters in the data.
"""

COMPANIES: dict[str, dict] = {
    "co_retail": {"time_fields": {"sales": "saleDate"}},
}
