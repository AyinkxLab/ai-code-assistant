"""Tests for the AI review service: parsing, bounds, and structured findings."""

import json

from app.extensions import db
from app.models import Project, ProjectFile, ReviewFinding, User, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY
from app.services import reviews


def _ready_project(files):
    user = User(username="revsvcuser", email="revsvcuser@example.com")
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    workspace = Workspace(user_id=user.id, name="Svc workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Svc project",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()
    for path, content in files:
        db.session.add(
            ProjectFile(
                project_id=project.id,
                path=path,
                size=len(content),
                is_binary=False,
                content=content,
            )
        )
    db.session.commit()
    return project


class FakeProvider:
    def __init__(self, text):
        self.text = text

    def complete(self, messages, *, stream=False):
        return self.text


JSON_REPLY = json.dumps(
    {
        "summary": {
            "overall_assessment": "Solid overall.",
            "important_findings": ["One high risk found."],
            "suggested_improvements": ["Add tests."],
            "testing_recommendations": ["Cover edge cases."],
            "security_concerns": ["None observed."],
            "performance_concerns": ["Fine."],
            "files_affected": ["app/main.py"],
        },
        "findings": [
            {
                "file": "app/main.py",
                "line": 4,
                "severity": "critical",
                "category": "bug",
                "explanation": "Null dereference when data is missing.",
                "recommendation": "Guard against None.",
                "confidence": "confirmed",
            },
            {
                "file": "app/main.py",
                "line": 9,
                "severity": "informational",
                "category": "other",
                "explanation": "Style note only.",
                "recommendation": "",
                "confidence": "suggestion",
            },
        ],
    }
)

QUALITY_JSON_REPLY = json.dumps(
    {
        "summary": {"overall_assessment": "Mixed quality."},
        "findings": [
            {
                "file": "app/main.py",
                "line": 3,
                "severity": "high",
                "category": "readability",
                "explanation": "Deeply nested conditionals hurt readability.",
                "recommendation": "Extract a helper.",
                "confidence": "confirmed",
            },
            {
                "file": "app/legacy.py",
                "line": 1,
                "severity": "medium",
                "category": "dead-code",
                "explanation": "An unreachable branch is never executed.",
                "recommendation": "Delete the dead branch.",
                "confidence": "potential",
            },
            {
                "file": "app/dup.py",
                "severity": "medium",
                "category": "duplication",
                "explanation": "Duplicated parsing logic.",
                "recommendation": "Reuse a single function.",
                "confidence": "confirmed",
            },
        ],
    }
)

TESTS_JSON_REPLY = json.dumps(
    {
        "summary": {"overall_assessment": "Tests need work."},
        "findings": [
            {
                "file": "tests/test_app.py",
                "line": 5,
                "severity": "high",
                "category": "coverage-gap",
                "explanation": "The error path is never exercised.",
                "recommendation": "Add a test for the failure branch.",
                "confidence": "confirmed",
            },
            {
                "file": "tests/test_app.py",
                "line": 12,
                "severity": "medium",
                "category": "missing-assertion",
                "explanation": "The test calls the function but asserts nothing.",
                "recommendation": "Assert the returned value.",
                "confidence": "confirmed",
            },
            {
                "file": "tests/test_worker.py",
                "severity": "medium",
                "category": "flaky-test",
                "explanation": "The test depends on a real timer sleep.",
                "recommendation": "Inject a clock.",
                "confidence": "potential",
            },
        ],
    }
)


class TestParseReviewResponse:
    def test_parses_json_findings(self, app):
        result = reviews.parse_review_response(JSON_REPLY, kind="pr")
        assert result["error"] is None
        assert result["summary"]["overall_assessment"] == "Solid overall."
        assert len(result["findings"]) == 2
        finding = result["findings"][0]
        assert finding["severity"] == "critical"
        assert finding["file"] == "app/main.py"
        assert finding["line"] == 4
        assert finding["confidence"] == "confirmed"

    def test_threshold_drops_low_severity(self, app):
        result = reviews.parse_review_response(JSON_REPLY, kind="pr", threshold="high")
        severities = [f["severity"] for f in result["findings"]]
        assert severities == ["critical"]

    def test_markdown_fenced_json(self, app):
        fenced = "```json\n" + JSON_REPLY + "\n```"
        result = reviews.parse_review_response(fenced, kind="pr")
        assert len(result["findings"]) == 2

    def test_unstructured_text_falls_back(self, app):
        result = reviews.parse_review_response("No JSON here, just prose.", kind="pr")
        assert result["findings"] == []
        assert "raw" in result["summary"]

    def test_invalid_finding_entries_skipped(self, app):
        payload = {
            "summary": {"overall_assessment": "x"},
            "findings": [
                "not-a-dict",
                {"severity": "high"},
                {"explanation": "   "},
                {"file": "a.py", "line": "nope", "explanation": "ok", "category": "bogus"},
            ],
        }
        result = reviews.parse_review_response(json.dumps(payload), kind="pr")
        assert len(result["findings"]) == 1
        finding = result["findings"][0]
        assert finding["file"] == "a.py"
        assert finding["line"] is None
        assert finding["category"] == "other"
        assert finding["severity"] == "medium"

    def test_categories_scoped_by_kind(self, app):
        payload = {
            "findings": [{"explanation": "x", "category": "missing-tests"}],
        }
        pr_result = reviews.parse_review_response(json.dumps(payload), kind="pr")
        tests_result = reviews.parse_review_response(json.dumps(payload), kind="tests")
        assert pr_result["findings"][0]["category"] == "other"
        assert tests_result["findings"][0]["category"] == "missing-tests"

    def test_quality_categories_include_readability_and_dead_code(self, app):
        payload = {
            "findings": [
                {"explanation": "x", "category": "readability"},
                {"explanation": "y", "category": "dead-code"},
                {"explanation": "z", "category": "bug"},
            ]
        }
        result = reviews.parse_review_response(json.dumps(payload), kind="quality")
        categories = [f["category"] for f in result["findings"]]
        assert categories == ["readability", "dead-code", "other"]

    def test_tests_categories_include_new_test_concerns(self, app):
        payload = {
            "findings": [
                {"explanation": "x", "category": "coverage-gap"},
                {"explanation": "y", "category": "missing-assertion"},
                {"explanation": "z", "category": "flaky-test"},
                {"explanation": "w", "category": "readability"},
            ]
        }
        result = reviews.parse_review_response(json.dumps(payload), kind="tests")
        assert [f["category"] for f in result["findings"]] == [
            "coverage-gap",
            "missing-assertion",
            "flaky-test",
            "other",
        ]


def _pr_file(name, patch="x", status="modified", additions=1, deletions=0):
    return {
        "filename": name,
        "patch": patch,
        "status": status,
        "additions": additions,
        "deletions": deletions,
    }


class TestBuildPrContext:
    def test_language_filter(self, app):
        config = {"languages": "py,ts", "max_files": 10, "max_context_chars": 20000}
        files = [
            _pr_file("app.py"),
            _pr_file("app.js"),
            _pr_file("README.md", status="added", additions=2),
        ]
        pr = {
            "number": 1,
            "title": "t",
            "state": "open",
            "merged": False,
            "author": "a",
            "base": "main",
            "head": "feat",
        }
        context = reviews.build_pr_context(pr, files, config)
        assert "app.py" in context["files_text"]
        assert "README.md" not in context["files_text"]

    def test_max_files_bounds_context(self, app):
        config = {"languages": None, "max_files": 2, "max_context_chars": 50000}
        files = [_pr_file(f"f{i}.py") for i in range(6)]
        context = reviews.build_pr_context({"number": 1}, files, config)
        assert context["selected_count"] == 2
        assert context["total_count"] == 6
        assert "only 2 of 6 changed files" in context["files_text"]

    def test_detects_test_files(self, app):
        config = {"languages": None, "max_files": 10, "max_context_chars": 50000}
        files = [_pr_file("tests/test_app.py", status="added"), _pr_file("app.py")]
        context = reviews.build_pr_context({"number": 1}, files, config)
        assert context["test_files"] == ["tests/test_app.py"]


class TestContextBounding:
    def test_clip_never_exceeds_limit(self, app):
        assert len(reviews._clip("x" * 1000, 100)) <= 100
        assert reviews._clip("short", 100) == "short"
        assert len(reviews._clip("x" * 100, 0)) == 0

    def test_pr_context_respects_char_budget_including_note(self, app):
        config = {"languages": None, "max_files": 3, "max_context_chars": 3000}
        files = [_pr_file(f"f{i}.py", patch="x" * 5000) for i in range(6)]
        context = reviews.build_pr_context({"number": 1}, files, config)
        assert len(context["files_text"]) <= 3000
        assert "only 3 of 6 changed files" in context["files_text"]

    def test_project_context_respects_char_budget(self, app):
        project = _ready_project([("app/a.py", "x" * 5000), ("app/b.py", "y" * 5000)])
        config = {
            "languages": None,
            "max_files": 40,
            "max_context_chars": 2000,
            "severity_threshold": "low",
        }
        context = reviews._project_context(project, config, "quality")
        assert len(context["blocks"]) <= 2000

    def test_project_context_language_filter(self, app):
        project = _ready_project(
            [("app/a.py", "python"), ("web/b.js", "js"), ("README.md", "markdown")]
        )
        config = {"languages": "py", "max_files": 40, "max_context_chars": 20000}
        context = reviews._project_context(project, config, "quality")
        assert "app/a.py" in context["blocks"]
        assert "web/b.js" not in context["blocks"]
        assert "README.md" not in context["blocks"]

    def test_project_context_max_files(self, app):
        project = _ready_project([(f"f{i}.py", "x") for i in range(5)])
        config = {"languages": None, "max_files": 2, "max_context_chars": 20000}
        context = reviews._project_context(project, config, "quality")
        assert context["count"] == 2
        assert context["blocks"].count("```") == 4


class TestAnalyzeCodeQuality:
    def test_structured_findings_cover_named_concerns(self, app, monkeypatch):
        project = _ready_project([("app/main.py", "def f():\n    pass\n")])
        monkeypatch.setattr(reviews, "get_provider", lambda: FakeProvider(QUALITY_JSON_REPLY))
        config = {
            "languages": None,
            "max_files": 40,
            "max_context_chars": 40000,
            "severity_threshold": "low",
        }
        result = reviews.analyze_code_quality(project, config)
        assert result["error"] is None
        categories = {f["category"] for f in result["findings"]}
        assert {"readability", "dead-code", "duplication"} <= categories

    def test_review_project_quality_delegates_to_analyze_code_quality(self, app, monkeypatch):
        project = _ready_project([("app/main.py", "x")])
        monkeypatch.setattr(reviews, "get_provider", lambda: FakeProvider(QUALITY_JSON_REPLY))
        config = {
            "languages": None,
            "max_files": 40,
            "max_context_chars": 40000,
            "severity_threshold": "low",
        }
        assert reviews.review_project(project, "quality", config) == reviews.analyze_code_quality(
            project, config
        )


class TestAnalyzeTests:
    def test_structured_findings_cover_test_concerns(self, app, monkeypatch):
        project = _ready_project([("tests/test_app.py", "def test_x():\n    pass\n")])
        monkeypatch.setattr(reviews, "get_provider", lambda: FakeProvider(TESTS_JSON_REPLY))
        config = {
            "languages": None,
            "max_files": 40,
            "max_context_chars": 40000,
            "severity_threshold": "low",
        }
        result = reviews.analyze_tests(project, config)
        assert result["error"] is None
        categories = {f["category"] for f in result["findings"]}
        assert {"coverage-gap", "missing-assertion", "flaky-test"} <= categories

    def test_review_project_tests_delegates_to_analyze_tests(self, app, monkeypatch):
        project = _ready_project([("app/main.py", "x")])
        monkeypatch.setattr(reviews, "get_provider", lambda: FakeProvider(TESTS_JSON_REPLY))
        config = {
            "languages": None,
            "max_files": 40,
            "max_context_chars": 40000,
            "severity_threshold": "low",
        }
        assert reviews.review_project(project, "tests", config) == reviews.analyze_tests(
            project, config
        )


class TestReviewRun:
    def test_review_project_structured(self, app, monkeypatch):
        project = _ready_project([("app/main.py", "def f():\n    pass\n")])
        monkeypatch.setattr(reviews, "get_provider", lambda: FakeProvider(JSON_REPLY))
        config = {
            "languages": None,
            "max_files": 40,
            "max_context_chars": 40000,
            "severity_threshold": "low",
            "testing_focus": True,
            "security_focus": True,
            "performance_focus": True,
        }
        result = reviews.review_project(project, "quality", config)
        assert result["error"] is None
        # The critical finding survives; the informational one is below the
        # "low" severity threshold and is correctly dropped.
        assert len(result["findings"]) == 1
        assert result["findings"][0]["severity"] == "critical"

    def test_review_project_unknown_kind_defaults_to_quality(self, app, monkeypatch):
        project = _ready_project([("app/main.py", "x")])
        monkeypatch.setattr(reviews, "get_provider", lambda: FakeProvider(JSON_REPLY))
        config = {
            "languages": None,
            "max_files": 40,
            "max_context_chars": 40000,
            "severity_threshold": "low",
        }
        result = reviews.review_project(project, "bogus", config)
        assert result["findings"]

    def test_review_pull_request(self, app, monkeypatch):
        monkeypatch.setattr(reviews, "get_provider", lambda: FakeProvider(JSON_REPLY))
        pr = {
            "number": 3,
            "title": "Add feature",
            "body": "Closes #2",
            "state": "open",
            "merged": False,
        }
        files = [_pr_file("app/main.py", patch="---\n+++\n+def f()")]
        config = {
            "languages": None,
            "max_files": 40,
            "max_context_chars": 40000,
            "severity_threshold": "low",
            "security_focus": True,
            "performance_focus": True,
        }
        result = reviews.review_pull_request(pr, files, config)
        assert result["findings"][0]["file"] == "app/main.py"

    def test_provider_error_reported(self, app, monkeypatch):
        from app.services.llm import LLMProviderError

        def boom(*args, **kwargs):
            raise LLMProviderError("no key")

        project = _ready_project([("app/main.py", "x")])
        monkeypatch.setattr(reviews, "get_provider", boom)
        config = {
            "languages": None,
            "max_files": 40,
            "max_context_chars": 40000,
            "severity_threshold": "low",
        }
        result = reviews.review_project(project, "quality", config)
        assert result["error"]
        assert result["findings"] == []

    def test_is_test_path(self):
        assert reviews.is_test_path("tests/test_a.py")
        assert reviews.is_test_path("src/test_a.py")
        assert reviews.is_test_path("pkg/tests/tests_b.py")
        assert not reviews.is_test_path("app/main.py")


class CapturingProvider:
    def __init__(self, text):
        self.text = text
        self.messages = None

    def complete(self, messages, *, stream=False):
        self.messages = messages
        return self.text


STRUCTURED_FINDING_FIELDS = frozenset(
    {"file", "line", "severity", "category", "explanation", "recommendation", "confidence"}
)


class TestStructuredFindingContract:
    """Regression tests locking the structured-findings contract (#110).

    Every review kind returns findings with severity, category, confidence, and
    a file/line location, and never stores raw repository content or arbitrary
    model-supplied fields.
    """

    def test_security_prompt_lists_full_category_vocabulary(self, app, monkeypatch):
        project = _ready_project([("app/main.py", "import os\n")])
        provider = CapturingProvider(JSON_REPLY)
        monkeypatch.setattr(reviews, "get_provider", lambda: provider)
        config = {
            "languages": None,
            "max_files": 40,
            "max_context_chars": 40000,
            "severity_threshold": "low",
        }
        reviews.review_project(project, "security", config)
        prompt = provider.messages[-1]["content"]
        for category in (
            "authorization",
            "injection",
            "unsafe-dependencies",
            "information-exposure",
            "insecure-config",
        ):
            assert category in prompt

    def test_findings_only_expose_structured_fields(self, app):
        payload = {
            "summary": {},
            "findings": [
                {
                    "explanation": "User input reaches the query builder.",
                    "severity": "high",
                    "category": "injection",
                    "confidence": "confirmed",
                    "file": "app/db.py",
                    "line": 12,
                    # Model-controlled extras must be dropped, not persisted.
                    "raw_content": "def run(q): ...",
                    "code": "SELECT * FROM users",
                }
            ],
        }
        finding = reviews.parse_review_response(json.dumps(payload), kind="security")["findings"][0]
        assert set(finding) == STRUCTURED_FINDING_FIELDS
        assert "raw_content" not in finding
        assert "code" not in finding

    def test_every_kind_returns_structured_location_and_vocabulary(self, app):
        for kind, category in (
            ("pr", "bug"),
            ("quality", "readability"),
            ("security", "injection"),
            ("tests", "coverage-gap"),
        ):
            payload = {
                "findings": [
                    {
                        "explanation": "e",
                        "severity": "high",
                        "category": category,
                        "confidence": "confirmed",
                        "file": "a.py",
                        "line": 3,
                    }
                ]
            }
            finding = reviews.parse_review_response(json.dumps(payload), kind=kind)["findings"][0]
            assert finding["severity"] in reviews.SEVERITIES
            assert finding["confidence"] in reviews.CONFIDENCES
            assert finding["category"] == category
            assert finding["file"] == "a.py"
            assert finding["line"] == 3

    def test_labels_instructed_consistently_for_pr_and_project(self, app, monkeypatch):
        project = _ready_project([("app/main.py", "x")])
        provider = CapturingProvider(JSON_REPLY)
        monkeypatch.setattr(reviews, "get_provider", lambda: provider)
        config = {
            "languages": None,
            "max_files": 40,
            "max_context_chars": 40000,
            "severity_threshold": "low",
            "security_focus": True,
            "performance_focus": True,
        }

        reviews.review_project(project, "security", config)
        project_system = provider.messages[0]["content"]
        assert "[CONFIRMED]" in project_system
        assert "[SUGGESTION]" in project_system

        provider.messages = None
        reviews.review_pull_request({"number": 1, "title": "t"}, [_pr_file("app/x.py")], config)
        pr_system = provider.messages[0]["content"]
        assert "[CONFIRMED]" in pr_system
        assert "[SUGGESTION]" in pr_system

    def test_finding_serialization_has_no_raw_content(self, app):
        finding = ReviewFinding(
            review_id=1,
            file="app/db.py",
            line=12,
            severity="high",
            category="injection",
            explanation="e",
            confidence="confirmed",
        )
        serialized = finding.to_dict()
        assert "raw_content" not in serialized
        assert {"file", "line", "severity", "category", "confidence"} <= set(serialized)
