"""Regression tests for the quality metrics service (#122).

Metrics must be computed strictly from the provided ``Review``/``ReviewFinding``
rows: an empty dataset yields zeros (nothing is fabricated), counts match the
rows exactly, and derived groupings stay consistent with the stored data.
"""

from datetime import UTC, datetime, timedelta

from app.services.metrics import SEVERITY_ORDER, review_metrics


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


class FakeReview:
    def __init__(self, id, *, kind="quality", source="project", status="completed", age_days=0):
        self.id = id
        self.kind = kind
        self.source = source
        self.status = status
        self.created_at = _now() - timedelta(days=age_days)


class FakeFinding:
    def __init__(
        self,
        review_id,
        *,
        severity="low",
        category="style",
        confidence="confirmed",
        file="app.py",
        addressed=False,
    ):
        self.review_id = review_id
        self.severity = severity
        self.category = category
        self.confidence = confidence
        self.file = file
        self.addressed = addressed


class TestMetricsFromStoredRows:
    def test_empty_dataset_is_all_zero(self):
        metrics = review_metrics([], [])
        assert metrics["total_reviews"] == 0
        assert metrics["last_review_at"] is None
        assert metrics["by_status"] == {}
        assert metrics["by_kind"] == {}
        assert metrics["by_source"] == {}
        assert metrics["reviews_last_7_days"] == 0
        assert metrics["reviews_last_30_days"] == 0
        f = metrics["findings"]
        assert f["total"] == 0
        assert f["by_severity"] == dict.fromkeys(SEVERITY_ORDER, 0)
        assert f["by_category"] == {}
        assert f["by_confidence"] == {}
        assert f["by_file"] == {}
        assert f["addressed"] == 0
        assert f["high_risk"] == 0
        assert f["unaddressed_high_risk"] == 0

    def test_counts_match_stored_rows(self):
        reviews = [
            FakeReview(1, kind="quality", source="project", status="completed"),
            FakeReview(2, kind="security", source="project", status="completed"),
            FakeReview(3, kind="quality", source="github", status="failed"),
        ]
        findings = [
            FakeFinding(1, severity="critical", category="injection", confidence="confirmed"),
            FakeFinding(1, severity="high", category="injection", confidence="potential"),
            FakeFinding(2, severity="low", category="style", confidence="suggestion"),
        ]
        metrics = review_metrics(reviews, findings)
        assert metrics["total_reviews"] == 3
        assert metrics["by_status"] == {"completed": 2, "failed": 1}
        assert metrics["by_kind"] == {"quality": 2, "security": 1}
        assert metrics["by_source"] == {"project": 2, "github": 1}
        f = metrics["findings"]
        assert f["total"] == 3
        assert f["by_category"] == {"injection": 2, "style": 1}
        assert f["by_confidence"] == {"confirmed": 1, "potential": 1, "suggestion": 1}
        assert f["by_kind"] == {"quality": 2, "security": 1}

    def test_high_risk_and_addressed_counts(self):
        findings = [
            FakeFinding(1, severity="critical", addressed=False),
            FakeFinding(1, severity="high", addressed=True),
            FakeFinding(1, severity="medium", addressed=False),
            FakeFinding(1, severity="low", addressed=True),
        ]
        metrics = review_metrics([FakeReview(1)], findings)["findings"]
        assert metrics["high_risk"] == 2
        assert metrics["unaddressed_high_risk"] == 1
        assert metrics["addressed"] == 2
        assert metrics["by_severity"]["critical"] == 1
        assert metrics["by_severity"]["informational"] == 0

    def test_findings_without_a_known_review_are_unknown_kind(self):
        metrics = review_metrics([FakeReview(1, kind="quality")], [FakeFinding(99)])
        assert metrics["findings"]["by_kind"] == {"unknown": 1}

    def test_by_file_is_limited_to_top_ten(self):
        findings = [FakeFinding(1, file=f"file_{i}.py") for i in range(12)]
        metrics = review_metrics([FakeReview(1)], findings)
        assert len(metrics["findings"]["by_file"]) == 10

    def test_recent_windows_use_row_timestamps(self):
        reviews = [
            FakeReview(1, age_days=1),
            FakeReview(2, age_days=10),
            FakeReview(3, age_days=40),
        ]
        metrics = review_metrics(reviews, [])
        assert metrics["reviews_last_7_days"] == 1
        assert metrics["reviews_last_30_days"] == 2

    def test_removing_rows_changes_metrics(self):
        reviews = [FakeReview(1), FakeReview(2)]
        findings = [FakeFinding(1), FakeFinding(2)]
        before = review_metrics(reviews, findings)
        after = review_metrics(reviews[1:], findings[1:])
        assert before["total_reviews"] == 2
        assert after["total_reviews"] == 1
        assert after["findings"]["total"] == 1

    def test_last_review_at_is_newest_row(self):
        older = FakeReview(1, age_days=5)
        newer = FakeReview(2, age_days=1)
        metrics = review_metrics([newer, older], [])
        assert metrics["last_review_at"] == newer.created_at.isoformat()
