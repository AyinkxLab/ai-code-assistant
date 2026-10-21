"""Marketplace search and filter tests (#378).

Verifies the literal, case-insensitive `q` / `capability` / `tag` filtering used
by ``GET /plugins/api/workspaces/<id>/marketplace``.
"""

from app.services.plugin_catalog import CatalogEntry, filter_catalog_entries


def _entry(
    plugin_id,
    *,
    name="",
    description="",
    author="",
    capabilities=(),
    tags=(),
    version="1.0.0",
):
    manifest = {
        "name": name or plugin_id,
        "description": description,
        "author": author,
        "capabilities": list(capabilities),
    }
    if tags:
        manifest["tags"] = list(tags)
    return CatalogEntry(
        plugin_id=plugin_id,
        version=version,
        source="catalog",
        integrity="sha256:" + "a" * 64,
        manifest=manifest,
    )


ENTRIES = [
    _entry(
        "stellar-payments",
        name="Stellar Payments",
        description="Send and receive XLM payments",
        author="Foundry",
        capabilities=["sign_transaction", "read_balance"],
        tags=["payments", "stellar"],
    ),
    _entry(
        "github-bot",
        name="GitHub Bot",
        description="Automate pull request reviews",
        author="OpenTools",
        capabilities=["read_repository"],
        tags=["github", "reviews"],
    ),
    _entry(
        "audit-logger",
        name="Audit Logger",
        description="Append-only audit events",
        author="Foundry",
        capabilities=["read_balance"],
        tags=["observability"],
    ),
]


def ids(entries):
    return [entry.plugin_id for entry in entries]


class TestNoFilters:
    def test_returns_all_entries_in_order(self):
        assert ids(filter_catalog_entries(ENTRIES)) == [
            "stellar-payments",
            "github-bot",
            "audit-logger",
        ]

    def test_blank_filters_are_ignored(self):
        assert len(filter_catalog_entries(ENTRIES, query="  ", capability="", tag=None)) == 3


class TestQuery:
    def test_matches_plugin_id_literally(self):
        assert ids(filter_catalog_entries(ENTRIES, query="payments")) == ["stellar-payments"]

    def test_matches_name_description_and_author(self):
        assert ids(filter_catalog_entries(ENTRIES, query="GITHUB BOT")) == ["github-bot"]
        assert ids(filter_catalog_entries(ENTRIES, query="pull request")) == ["github-bot"]
        assert ids(filter_catalog_entries(ENTRIES, query="foundry")) == [
            "stellar-payments",
            "audit-logger",
        ]

    def test_is_case_insensitive_and_literal(self):
        assert ids(filter_catalog_entries(ENTRIES, query="AuDiT")) == ["audit-logger"]
        assert filter_catalog_entries(ENTRIES, query="pay.*") == []


class TestCapability:
    def test_filters_by_exact_capability(self):
        assert ids(filter_catalog_entries(ENTRIES, capability="sign_transaction")) == [
            "stellar-payments"
        ]
        assert ids(filter_catalog_entries(ENTRIES, capability="READ_BALANCE")) == [
            "stellar-payments",
            "audit-logger",
        ]

    def test_unknown_capability_returns_nothing(self):
        assert filter_catalog_entries(ENTRIES, capability="manage_plugins") == []


class TestTag:
    def test_matches_explicit_tags(self):
        assert ids(filter_catalog_entries(ENTRIES, tag="reviews")) == ["github-bot"]
        assert ids(filter_catalog_entries(ENTRIES, tag="observability")) == ["audit-logger"]

    def test_a_tag_also_matches_an_identical_capability(self):
        assert ids(filter_catalog_entries(ENTRIES, tag="read_balance")) == [
            "stellar-payments",
            "audit-logger",
        ]


class TestCombinations:
    def test_filters_combine_with_and_semantics(self):
        assert ids(filter_catalog_entries(ENTRIES, query="audit", capability="read_balance")) == [
            "audit-logger"
        ]
        assert ids(
            filter_catalog_entries(
                ENTRIES, query="audit", capability="read_balance", tag="observability"
            )
        ) == ["audit-logger"]
        assert (
            ids(
                filter_catalog_entries(
                    ENTRIES, query="audit", capability="read_balance", tag="stellar"
                )
            )
            == []
        )

    def test_no_combination_match_returns_empty(self):
        assert filter_catalog_entries(ENTRIES, query="github", capability="read_balance") == []
