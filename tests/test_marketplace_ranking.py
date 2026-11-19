"""Marketplace search ranking, categories and tags (#693)."""

import json

from app.services.marketplace import (
    RANKING_WEIGHTS,
    MarketplaceEntry,
    filter_entries,
    marketplace_categories,
    rank_entries,
)

HASH = "a" * 64


def _entry(
    plugin_id,
    *,
    name=None,
    description="",
    author="Acme",
    categories=(),
    tags=(),
    publisher=None,
):
    signature = (
        {"publisher": publisher, "algorithm": "ed25519", "value": "x"} if publisher else None
    )
    return MarketplaceEntry(
        plugin_id=plugin_id,
        name=name or plugin_id,
        version="1.0.0",
        description=description,
        author=author,
        capabilities=("PROJECT_READ",),
        permissions=(),
        dependencies=(),
        compatibility=None,
        integrity="sha256:" + HASH,
        source=f"https://plugins.example.com/{plugin_id}.zip",
        signature=signature,
        categories=categories,
        tags=tags,
    )


def _ids(entries):
    return [entry.plugin_id for entry in entries]


class TestRanking:
    def test_weights_are_documented_and_sum_to_one(self):
        assert set(RANKING_WEIGHTS) == {"relevance", "trust", "installs"}
        assert abs(sum(RANKING_WEIGHTS.values()) - 1.0) < 1e-9

    def test_relevance_ranks_a_name_match_above_a_description_match(self):
        entries = [
            _entry("zzz", name="Unrelated", description="stellar payments helper"),
            _entry("aaa", name="Stellar Payments"),
        ]

        assert _ids(rank_entries(entries, query="stellar payments")) == ["aaa", "zzz"]

    def test_trust_breaks_a_relevance_tie(self):
        entries = [
            _entry("aaa", name="Payments", publisher="acme"),
            _entry("bbb", name="Payments", publisher="unknown"),
        ]

        assert _ids(rank_entries(entries, query="payments", trusted_publishers={"acme"})) == [
            "aaa",
            "bbb",
        ]

    def test_install_count_breaks_a_remaining_tie(self):
        entries = [_entry("aaa", name="Payments"), _entry("bbb", name="Payments")]

        assert _ids(rank_entries(entries, query="payments", install_counts={"bbb": 25})) == [
            "bbb",
            "aaa",
        ]

    def test_ordering_is_stable_across_repeated_calls_and_input_order(self):
        entries = [_entry("bbb", name="Payments"), _entry("aaa", name="Payments")]

        first = _ids(rank_entries(entries, query="payments"))
        second = _ids(rank_entries(list(reversed(entries)), query="payments"))

        assert first == ["aaa", "bbb"]
        assert second == ["aaa", "bbb"]

    def test_empty_input_returns_empty_result(self):
        assert rank_entries([], query="anything") == []


class TestFiltersAndFacets:
    def test_filter_by_category_and_tag(self):
        from app.services.marketplace import MarketplaceCatalog

        catalog = MarketplaceCatalog(
            [
                _entry("alpha", categories=("stellar",), tags=("payments",)),
                _entry("beta", categories=("github",), tags=("reviews",)),
            ]
        )

        assert _ids(filter_entries(catalog, category="stellar")) == ["alpha"]
        assert _ids(filter_entries(catalog, tag="reviews")) == ["beta"]
        assert filter_entries(catalog, category="missing") == []

    def test_category_and_tag_facets_are_sorted_with_counts(self):
        entries = [
            _entry("alpha", categories=("stellar",), tags=("payments", "xlm")),
            _entry("beta", categories=("stellar", "github"), tags=("payments",)),
        ]

        categories, tags = marketplace_categories(entries)

        assert categories == [
            {"name": "github", "count": 1},
            {"name": "stellar", "count": 2},
        ]
        assert tags == [
            {"name": "payments", "count": 2},
            {"name": "xlm", "count": 1},
        ]

    def test_empty_catalog_has_empty_facets(self):
        assert marketplace_categories([]) == ([], [])


def _catalog(tmp_path, entries):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"version": 1, "plugins": entries}), encoding="utf-8")
    return path


def _catalog_entry(plugin_id, **manifest_overrides):
    manifest = {
        "id": plugin_id,
        "name": plugin_id.replace("-", " ").title(),
        "version": "1.0.0",
        "description": "A plugin",
        "author": "Acme",
        "entry_point": "plugins.test:TestPlugin",
        "capabilities": ["PROJECT_READ"],
        **manifest_overrides,
    }
    return {
        "id": plugin_id,
        "version": "1.0.0",
        "source": f"https://plugins.example.com/{plugin_id}.zip",
        "integrity": "sha256:" + HASH,
        "manifest": manifest,
    }


class TestMarketplaceApi:
    def test_q_and_category_filters_rank_results_and_expose_facets(
        self, app, client, make_user, login, tmp_path
    ):
        make_user(username="member", email="member@example.com")
        app.config["PLUGIN_MARKETPLACE_CATALOG"] = str(
            _catalog(
                tmp_path,
                [
                    _catalog_entry(
                        "stellar-tools",
                        category="stellar",
                        tags=["payments"],
                        description="Soroban helpers",
                    ),
                    _catalog_entry(
                        "github-bot",
                        categories=["github"],
                        description="Stellar review automation",
                    ),
                ],
            )
        )
        login(email="member@example.com")

        payload = client.get("/plugins/api/marketplace?q=stellar&category=stellar").get_json()

        assert [entry["id"] for entry in payload["entries"]] == ["stellar-tools"]
        assert payload["entries"][0]["category"] == "stellar"
        assert payload["entries"][0]["categories"] == ["stellar"]
        assert payload["entries"][0]["tags"] == ["payments"]
        assert payload["ordering"] == ["relevance", "trust", "installs", "id", "version"]

        facets = client.get("/plugins/api/marketplace/categories").get_json()
        assert {"name": "stellar", "count": 1} in facets["categories"]
        assert {"name": "payments", "count": 1} in facets["tags"]

    def test_unknown_filter_returns_empty_without_error(
        self, app, client, make_user, login, tmp_path
    ):
        make_user(username="member", email="member@example.com")
        app.config["PLUGIN_MARKETPLACE_CATALOG"] = str(
            _catalog(tmp_path, [_catalog_entry("alpha", category="stellar")])
        )
        login(email="member@example.com")

        response = client.get("/plugins/api/marketplace?category=missing")

        assert response.status_code == 200
        assert response.get_json()["entries"] == []
