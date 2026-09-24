"""Tests for the project health dashboard additions (#105): CI-config detection
and the clearly-labelled coverage estimate."""

from app.extensions import db
from app.models import Project, ProjectFile, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY
from app.services.health import coverage_estimate, detect_ci_files


def _project_for(user, files):
    workspace = Workspace(user_id=user.id, name="Health workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Health project",
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


class TestDetectCiFiles:
    def test_detects_common_ci_files(self):
        paths = [
            ".github/workflows/ci.yml",
            ".github/workflows/release.yaml",
            ".gitlab-ci.yml",
            ".circleci/config.yml",
            "Jenkinsfile",
            "src/app.py",
            "README.md",
        ]
        found = detect_ci_files(paths)
        assert ".github/workflows/ci.yml" in found
        assert ".github/workflows/release.yaml" in found
        assert ".gitlab-ci.yml" in found
        assert ".circleci/config.yml" in found
        assert "Jenkinsfile" in found
        assert "src/app.py" not in found
        assert found == sorted(found)

    def test_no_ci_files(self):
        assert detect_ci_files(["src/app.py", "README.md"]) == []


class TestCoverageEstimate:
    def test_ratio_and_label(self):
        estimate = coverage_estimate(["src/a.py", "src/b.py", "src/c.py", "tests/test_a.py"])
        assert estimate["estimate"] is True
        assert estimate["test_file_count"] == 1
        assert estimate["source_file_count"] == 3
        assert estimate["ratio"] == round(1 / 3, 3)
        assert estimate["label"] in {"low", "moderate", "good", "high"}
        assert "estimated" in estimate["note"].lower()

    def test_no_tests_is_none(self):
        estimate = coverage_estimate(["src/a.py"])
        assert estimate["test_file_count"] == 0
        assert estimate["label"] == "none"

    def test_no_source_is_unknown(self):
        estimate = coverage_estimate(["README.md"])
        assert estimate["source_file_count"] == 0
        assert estimate["ratio"] == 0.0
        assert estimate["label"] == "unknown"


class TestStatsRouteHealth:
    def test_stats_include_ci_and_coverage(self, client, app, make_user, login):
        user = make_user()
        login()
        project = _project_for(
            user,
            [
                ("src/app.py", "print(1)"),
                ("src/util.py", "x = 1"),
                ("tests/test_app.py", "def test(): pass"),
                (".github/workflows/ci.yml", "on: push"),
                (".gitlab-ci.yml", "stages: []"),
            ],
        )
        response = client.get(f"/workspaces/api/projects/{project.id}/stats")
        assert response.status_code == 200
        data = response.get_json()
        assert ".github/workflows/ci.yml" in data["ci_files"]
        assert ".gitlab-ci.yml" in data["ci_files"]

        coverage = data["coverage_estimate"]
        assert coverage["estimate"] is True
        assert coverage["test_file_count"] == 1
        assert coverage["source_file_count"] == 2
