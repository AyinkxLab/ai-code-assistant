"""Tests for Phase 6 models: reviews, findings, config, and workspace members."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Project,
    ProjectFile,
    Review,
    ReviewConfig,
    ReviewFinding,
    User,
    Workspace,
    WorkspaceMember,
)
from app.models.project import SOURCE_ARCHIVE, STATUS_READY
from app.models.review import (
    PROJECT_REVIEW_KINDS,
    SOURCE_GITHUB_PR,
    STATUS_COMPLETED,
    STATUS_RUNNING,
)
from app.models.review_finding import (
    CONFIDENCES,
    PR_CATEGORIES,
    SEVERITIES,
    confidence_label,
)
from app.models.workspace_member import ROLE_CONTRIBUTOR, ROLE_VIEWER, VALID_ROLES


def _make_user(username="revuser", email="revuser@example.com"):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _make_workspace(user, name="Review workspace"):
    workspace = Workspace(user_id=user.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _make_project(user, workspace, name="Review project"):
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name=name,
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
        file_count=0,
        total_size_bytes=0,
    )
    db.session.add(project)
    db.session.commit()
    return project


class TestReviewModel:
    def test_review_defaults(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = _make_project(user, workspace)
        review = Review(
            user_id=user.id,
            project_id=project.id,
            source=SOURCE_GITHUB_PR,
            owner="octocat",
            repo="hello-world",
            pr_number=12,
        )
        db.session.add(review)
        db.session.commit()
        assert review.status == STATUS_RUNNING
        assert review.kind == "pr"
        assert review.findings_count == 0
        assert review.pr_title is None
        payload = review.to_dict()
        assert payload["source"] == SOURCE_GITHUB_PR
        assert payload["pr_number"] == 12
        assert payload["summary"] is None

    def test_review_summary_dict_parses_json(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = _make_project(user, workspace)
        review = Review(
            user_id=user.id,
            project_id=project.id,
            source="project",
            kind="quality",
            status=STATUS_COMPLETED,
            summary='{"overall_assessment": "looks good"}',
        )
        db.session.add(review)
        db.session.commit()
        assert review.summary_dict == {"overall_assessment": "looks good"}

    def test_review_summary_dict_invalid(self, app):
        user = _make_user()
        review = Review(user_id=user.id, source="project", summary="not-json{")
        assert review.summary_dict is None

    def test_review_created_at_set(self, app):
        user = _make_user()
        review = Review(user_id=user.id, source="project", kind="security")
        db.session.add(review)
        db.session.commit()
        assert review.created_at is not None
        assert review.updated_at is not None


class TestReviewFindingModel:
    def test_finding_defaults(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = _make_project(user, workspace)
        review = Review(user_id=user.id, project_id=project.id, source="project", kind="tests")
        db.session.add(review)
        db.session.commit()
        finding = ReviewFinding(
            review_id=review.id,
            file="app/main.py",
            line=3,
            severity="high",
            category="bug",
            explanation="Division by zero.",
            recommendation="Guard the divisor.",
            confidence="confirmed",
        )
        db.session.add(finding)
        db.session.commit()
        payload = finding.to_dict()
        assert payload["severity"] == "high"
        assert payload["addressed"] is False
        assert payload["file"] == "app/main.py"
        assert payload["line"] == 3

    def test_finding_vocab_constants(self, app):
        assert "critical" in SEVERITIES
        assert "tests" in PR_CATEGORIES
        assert "suggestion" in CONFIDENCES

    def test_finding_confidence_label(self, app):
        assert confidence_label("confirmed") == "[CONFIRMED]"
        assert confidence_label("potential") == "[SUGGESTION]"
        assert confidence_label("suggestion") == "[SUGGESTION]"
        assert confidence_label(None) == "[SUGGESTION]"

        confirmed = ReviewFinding(
            review_id=1,
            severity="high",
            category="bug",
            explanation="e",
            confidence="confirmed",
        )
        assert confirmed.confidence_label == "[CONFIRMED]"
        assert confirmed.to_dict()["confidence_label"] == "[CONFIRMED]"

        inferred = ReviewFinding(
            review_id=1,
            severity="low",
            category="other",
            explanation="e",
            confidence="potential",
        )
        assert inferred.confidence_label == "[SUGGESTION]"

    def test_findings_cascade_with_review(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = _make_project(user, workspace)
        review = Review(user_id=user.id, project_id=project.id, source="project", kind="quality")
        db.session.add(review)
        db.session.commit()
        db.session.add(
            ReviewFinding(review_id=review.id, explanation="one"),
        )
        db.session.add(
            ReviewFinding(review_id=review.id, explanation="two"),
        )
        db.session.commit()
        db.session.delete(review)
        db.session.commit()
        assert ReviewFinding.query.count() == 0

    def test_review_cascades_with_project(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = _make_project(user, workspace)
        review = Review(user_id=user.id, project_id=project.id, source="project", kind="quality")
        db.session.add(review)
        db.session.commit()
        project_id = project.id
        db.session.delete(project)
        db.session.commit()
        assert Review.query.filter_by(project_id=project_id).count() == 0


class TestReviewConfigModel:
    def test_config_created(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = _make_project(user, workspace)
        config = ReviewConfig(
            user_id=user.id,
            project_id=project.id,
            severity_threshold="high",
            max_files=5,
        )
        db.session.add(config)
        db.session.commit()
        payload = config.to_dict()
        assert payload["severity_threshold"] == "high"
        assert payload["enabled"] is True
        assert payload["max_files"] == 5

    def test_config_unique_per_user_project(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = _make_project(user, workspace)
        db.session.add(ReviewConfig(user_id=user.id, project_id=project.id))
        db.session.commit()
        db.session.add(ReviewConfig(user_id=user.id, project_id=project.id))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_config_cascades_with_project(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = _make_project(user, workspace)
        db.session.add(ReviewConfig(user_id=user.id, project_id=project.id))
        db.session.commit()
        db.session.delete(project)
        db.session.commit()
        assert ReviewConfig.query.count() == 0

    def test_project_review_config_relationship(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = _make_project(user, workspace)
        config = ReviewConfig(user_id=user.id, project_id=project.id)
        db.session.add(config)
        db.session.commit()
        assert project.review_config is config


class TestWorkspaceMemberModel:
    def test_member_created(self, app):
        owner = _make_user("owneruser", "owner@example.com")
        member = _make_user("memberuser", "member@example.com")
        workspace = _make_workspace(owner)
        membership = WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role=ROLE_VIEWER)
        db.session.add(membership)
        db.session.commit()
        payload = membership.to_dict()
        assert payload["username"] == "memberuser"
        assert payload["role"] == ROLE_VIEWER
        assert workspace.members == [membership]

    def test_valid_roles(self, app):
        assert VALID_ROLES == ("owner", "contributor", "viewer")
        assert ROLE_CONTRIBUTOR == "contributor"

    def test_member_unique_per_workspace_user(self, app):
        owner = _make_user("owner2", "owner2@example.com")
        member = _make_user("member2", "member2@example.com")
        workspace = _make_workspace(owner)
        db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=member.id))
        db.session.commit()
        db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=member.id))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_members_cascade_with_workspace(self, app):
        owner = _make_user("owner3", "owner3@example.com")
        member = _make_user("member3", "member3@example.com")
        workspace = _make_workspace(owner)
        db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=member.id))
        db.session.commit()
        db.session.delete(workspace)
        db.session.commit()
        assert WorkspaceMember.query.count() == 0


class TestProjectRelationships:
    def test_project_reviews_relationship(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = _make_project(user, workspace)
        db.session.add(Review(user_id=user.id, project_id=project.id, source="project"))
        db.session.commit()
        assert len(project.reviews) == 1
        assert PROJECT_REVIEW_KINDS == ("quality", "security", "tests")

    def test_project_files_still_work(self, app):
        user = _make_user()
        workspace = _make_workspace(user)
        project = _make_project(user, workspace)
        db.session.add(
            ProjectFile(
                project_id=project.id,
                path="app.py",
                size=4,
                is_binary=False,
                content="pass",
            )
        )
        db.session.commit()
        assert project.files.count() == 1
