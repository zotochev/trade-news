"""Read-only API: schema, filters, pagination, one item, sectors."""

import sqlalchemy as sa

from tests.test_admin import admin  # noqa: F401  (the fixture: admin app with two items)
from trade_news.db.schema import annotations, items


def test_openapi_and_docs(admin):  # noqa: F811
    spec = admin.get("/api/openapi.json").json()
    assert spec["info"]["title"] == "trade-news API"
    assert {"/news", "/news/{item_id}", "/sectors"} <= set(spec["paths"])
    assert "NewsItem" in spec["components"]["schemas"]
    assert admin.get("/api/docs").status_code == 200


def test_news_items_and_filters(admin):  # noqa: F811
    page = admin.get("/api/news").json()
    assert len(page["items"]) == 2 and page["next_cursor"] is None
    apple = next(i for i in page["items"] if i["main_asset"]["symbol"] == "AAPL")
    assert apple["summary"] == "Apple отчиталась лучше ожиданий"
    assert apple["url"] == "https://example.com/apple" and apple["event_type"] == "earnings"
    assert apple["sectors"] == [
        {"sector": "technology", "industry": "hardware", "name": "железо и электроника"}
    ]
    assert apple["relevance"]["type"] == "immediate" and apple["whale"] is None
    assert "body" not in apple  # the feed text is not redistributed

    def titles(qs):
        return [i["title"] for i in admin.get(f"/api/news?{qs}").json()["items"]]

    assert titles("asset=aapl") == ["Apple beats estimates"]
    assert titles("direction=bearish") == ["<script>alert(1)</script> Space startup news"]
    assert titles("sector=aerospace_defense") == titles("direction=bearish")
    assert titles("whale=true") == [] and len(titles("whale=false")) == 2
    assert titles("min_importance=5") == []
    assert admin.get("/api/news?sector=bogus").status_code == 400
    assert admin.get("/api/news?min_importance=9").status_code == 422


def test_pagination_walks_all_items_once(admin):  # noqa: F811
    seen, cursor = [], None
    while True:
        page = admin.get("/api/news", params={"limit": 1, "cursor": cursor} if cursor else
                         {"limit": 1}).json()  # fmt: skip
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 2
    assert admin.get("/api/news?cursor=garbage!").status_code == 400


def test_one_item_and_sectors(admin, engine):  # noqa: F811
    with engine.connect() as conn:
        item_id = conn.execute(sa.select(items.c.id).where(items.c.source_item_id == "1")).scalar()
    assert admin.get(f"/api/news/{item_id}").json()["title"] == "Apple beats estimates"
    assert admin.get("/api/news/999999").status_code == 404
    sectors = admin.get("/api/sectors").json()
    tech = next(s for s in sectors if s["key"] == "technology")
    assert (
        tech["cycle"] == "sensitive"
        and {"key": "semiconductors", "name": "полупроводники"} in (tech["industries"])
    )


def test_whale_block_with_partial_fields(admin, engine):  # noqa: F811
    from trade_news.annotation import whale

    with engine.begin() as conn:
        item_id = conn.execute(sa.select(items.c.id).where(items.c.source_item_id == "1")).scalar()
        aid = conn.execute(
            sa.select(annotations.c.id).where(annotations.c.item_id == item_id)
        ).scalar()
        # an insider whale carries no exchange / listing fields at all
        whale._store(conn, aid, {"whale": True, "whale_kind": "insider", "strength": "strong",
                                 "cluster_count": 2, "amount_usd": 620000.0})  # fmt: skip
    page = admin.get("/api/news?whale=true").json()
    (item,) = page["items"]
    assert item["whale"]["kind"] == "insider" and item["whale"]["exchange"] is None
    assert item["whale"]["cluster_count"] == 2
