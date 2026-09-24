"""Reviews routes: pages, review API, configuration, and quality metrics.

Layout
------
Pages (HTML)
    /reviews/                        review history list
    /reviews/<id>                    review detail (findings)
    /reviews/projects/<pid>          project reviews (run + history)
    /reviews/projects/<pid>/config   project review configuration

API (JSON, all scoped to the current user)
    /reviews/api/reviews                         list / run a review
    /reviews/api/reviews/<id>                    detail / delete
    /reviews/api/reviews/<id>/findings           findings list
    /reviews/api/reviews/findings/<id>           update a finding (addressed)
    /reviews/api/projects/<pid>/config           get / update project config
    /reviews/api/metrics                         quality dashboard metrics
"""

from __future__ import annotations

import json

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Project, Review, ReviewConfig, ReviewFinding, Workspace
from app.models.project import STATUS_READY
from app.models.review import (
    PROJECT_REVIEW_KINDS,
    SOURCE_GITHUB_PR,
    SOURCE_PROJECT,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
)
from app.models.review_finding import CATEGORIES_BY_KIND, SEVERITIES
from app.reviews import bp
from app.services import metrics as metrics_service
from app.services import reviews as reviews_service
from app.services.events import emit_event
from app.services.github import (
    GitHubError,
    GitHubInvalidError,
    get_github_client,
    github_error_payload,
    pull_request_payload,
    validate_full_name,
)

# --------------------------------------------------------------------------
# Ownership helpers
# --------------------------------------------------------------------------


def _get_review(review_id: int) -> Review:
    return Review.query.filter_by(id=review_id, user_id=current_user.id).first_or_404()


def _get_project(project_id: int) -> Project:
    return Project.query.filter_by(id=project_id, user_id=current_user.id).first_or_404()


# --------------------------------------------------------------------------
# Configuration helpers
# --------------------------------------------------------------------------


def _config_payload(row: ReviewConfig | None) -> dict:
    """Merge a stored config row with application defaults."""
    from flask import current_app as app

    defaults = {
        "kinds": app.config["REVIEW_KINDS"],
        "severity_threshold": app.config["REVIEW_SEVERITY_THRESHOLD"],
        "languages": None,
        "testing_focus": True,
        "security_focus": True,
        "performance_focus": True,
        "max_files": app.config["REVIEW_MAX_FILES"],
        "max_context_chars": app.config["REVIEW_MAX_CONTEXT_CHARS"],
        "enabled": True,
    }
    if row is None:
        return defaults
    payload = dict(defaults)
    for key in defaults:
        value = getattr(row, key)
        if value is not None:
            payload[key] = value
    return payload


def _configured_kinds(config: dict) -> list[str]:
    kinds = [k.strip() for k in (config.get("kinds") or "").split(",") if k.strip()]
    return kinds or list(PROJECT_REVIEW_KINDS)


def _effective_config(project: Project) -> dict:
    row = ReviewConfig.query.filter_by(user_id=current_user.id, project_id=project.id).first()
    return _config_payload(row)


_CONFIG_FIELDS = (
    "kinds",
    "severity_threshold",
    "languages",
    "testing_focus",
    "security_focus",
    "performance_focus",
    "max_files",
    "max_context_chars",
    "enabled",
)


def _apply_config(row: ReviewConfig, data: dict) -> str | None:
    """Apply validated config fields to ``row``; return an error string or None."""
    if "kinds" in data:
        kinds = [k.strip().lower() for k in str(data.get("kinds") or "").split(",") if k.strip()]
        if any(k not in PROJECT_REVIEW_KINDS for k in kinds):
            return "Unsupported review kind."
        row.kinds = ",".join(kinds) if kinds else None
    if "severity_threshold" in data:
        threshold = (data.get("severity_threshold") or "").strip().lower()
        if threshold and threshold not in SEVERITIES:
            return "Unsupported severity threshold."
        row.severity_threshold = threshold or None
    if "languages" in data:
        languages = str(data.get("languages") or "").strip()
        row.languages = languages[:500] or None
    for flag in ("testing_focus", "security_focus", "performance_focus", "enabled"):
        if flag in data:
            setattr(row, flag, bool(data[flag]))
    if "max_files" in data:
        try:
            max_files = int(data["max_files"])
        except (TypeError, ValueError):
            return "max_files must be an integer."
        if not 1 <= max_files <= 200:
            return "max_files must be between 1 and 200."
        row.max_files = max_files
    if "max_context_chars" in data:
        try:
            limit = int(data["max_context_chars"])
        except (TypeError, ValueError):
            return "max_context_chars must be an integer."
        if not 2000 <= limit <= 200000:
            return "max_context_chars must be between 2000 and 200000."
        row.max_context_chars = limit
    return None


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@bp.route("/")
@login_required
def index():
    """Review history list page."""
    return render_template("reviews/index.html")


@bp.route("/<int:review_id>")
@login_required
def detail(review_id: int):
    """Review detail page."""
    _get_review(review_id)
    return render_template("reviews/detail.html", review_id=review_id)


@bp.route("/projects/<int:project_id>")
@login_required
def project_page(project_id: int):
    """Project reviews page."""
    _get_project(project_id)
    return render_template("reviews/project.html", project_id=project_id)


@bp.route("/projects/<int:project_id>/config")
@login_required
def project_config_page(project_id: int):
    """Project review configuration page."""
    _get_project(project_id)
    return render_template("reviews/config.html", project_id=project_id)


# --------------------------------------------------------------------------
# API: reviews
# --------------------------------------------------------------------------


@bp.route("/api/reviews", methods=["GET"])
@login_required
def api_list_reviews():
    query = Review.query.filter_by(user_id=current_user.id)
    source = request.args.get("source")
    kind = request.args.get("kind")
    status = request.args.get("status")
    project_id = request.args.get("project_id", type=int)
    if source:
        query = query.filter_by(source=source)
    if kind:
        query = query.filter_by(kind=kind)
    if status:
        query = query.filter_by(status=status)
    if project_id:
        query = query.filter_by(project_id=project_id)
    reviews = query.order_by(Review.created_at.desc()).all()
    return jsonify([r.to_dict() for r in reviews])


@bp.route("/api/reviews", methods=["POST"])
@login_required
def api_create_review():
    """Run a new review (GitHub pull request or imported project)."""
    data = request.get_json(silent=True) or {}
    source = (data.get("source") or SOURCE_PROJECT).strip().lower()
    if source == SOURCE_GITHUB_PR:
        return _run_pr_review(data)
    if source == SOURCE_PROJECT:
        return _run_project_review(data)
    return jsonify({"error": "Unsupported review source."}), 400


def _run_pr_review(data: dict):
    try:
        full_name = validate_full_name(data.get("repo") or "")
    except GitHubInvalidError as exc:
        return jsonify(github_error_payload(exc)), 400
    try:
        number = int(data.get("pr_number"))
    except (TypeError, ValueError):
        return jsonify({"error": "A pull request number is required."}), 400

    project_id = data.get("project_id")
    project = _get_project(project_id) if project_id else None

    try:
        client = get_github_client()
    except GitHubError as exc:
        return jsonify(github_error_payload(exc)), 400

    try:
        pr_raw = client.get_pull_request(full_name, number)
        files = client.list_pull_request_files(full_name, number)
    except GitHubError as exc:
        return jsonify(github_error_payload(exc)), 502

    pr = pull_request_payload(pr_raw)
    config = _effective_config(project) if project else _config_payload(None)

    review = Review(
        user_id=current_user.id,
        project_id=project.id if project else None,
        source=SOURCE_GITHUB_PR,
        kind="pr",
        status=STATUS_RUNNING,
        owner=full_name.split("/")[0],
        repo=full_name.split("/")[1],
        pr_number=number,
        pr_title=pr.get("title"),
        base_ref=pr.get("base"),
        head_ref=pr.get("head"),
    )
    db.session.add(review)
    db.session.commit()
    try:
        result = reviews_service.review_pull_request(pr, files, config)
    except Exception as exc:
        result = {"summary": {}, "findings": [], "raw": "", "error": str(exc)}
    _save_result(review, result, config)
    emit_event(
        "review.completed",
        data={"review_id": review.id, "kind": review.kind, "status": review.status},
        workspace_id=project.workspace_id if project else None,
        user_id=current_user.id,
    )
    return jsonify(review.to_dict()), 201


def _run_project_review(data: dict):
    project = _get_project(data.get("project_id"))
    if project.status != STATUS_READY:
        return jsonify({"error": "This project has not finished indexing."}), 409
    kind = (data.get("kind") or "quality").strip().lower()
    if kind not in PROJECT_REVIEW_KINDS:
        return jsonify({"error": "Unsupported review kind."}), 400

    config = _effective_config(project)
    if not config["enabled"]:
        return jsonify({"error": "Reviewing is disabled for this project."}), 400
    if kind not in _configured_kinds(config):
        return jsonify({"error": "This review kind is not enabled for the project."}), 400

    review = Review(
        user_id=current_user.id,
        project_id=project.id,
        source=SOURCE_PROJECT,
        kind=kind,
        status=STATUS_RUNNING,
    )
    db.session.add(review)
    db.session.commit()
    try:
        result = reviews_service.review_project(project, kind, config)
    except Exception as exc:
        result = {"summary": {}, "findings": [], "raw": "", "error": str(exc)}
    _save_result(review, result, config)
    emit_event(
        "review.completed",
        data={"review_id": review.id, "kind": review.kind, "status": review.status},
        workspace_id=project.workspace_id,
        user_id=current_user.id,
    )
    return jsonify(review.to_dict()), 201


def _save_result(review: Review, result: dict, config: dict) -> None:
    """Persist a completed (or failed) review together with its findings."""
    review.summary = json.dumps(result.get("summary") or {})
    review.config = json.dumps(config)
    findings = [
        ReviewFinding(review_id=review.id, **finding) for finding in result.get("findings") or []
    ]
    review.findings_count = len(findings)
    db.session.add_all(findings)
    error = result.get("error")
    if error:
        review.status = STATUS_FAILED
        review.error_message = str(error)[:2000]
    else:
        review.status = STATUS_COMPLETED
        review.error_message = None
    db.session.commit()


@bp.route("/api/reviews/<int:review_id>", methods=["GET"])
@login_required
def api_review_detail(review_id: int):
    review = _get_review(review_id)
    payload = review.to_dict()
    # Expose the category vocabulary for this review kind so the detail page can
    # offer a category filter (#118).
    payload["categories"] = list(CATEGORIES_BY_KIND.get(review.kind, ("other",)))
    return jsonify(payload)


@bp.route("/api/reviews/<int:review_id>", methods=["DELETE"])
@login_required
def api_delete_review(review_id: int):
    """Delete a review and its findings (owner only; findings cascade)."""
    review = _get_review(review_id)
    workspace_id = review.project.workspace_id if review.project_id else None
    data = {"review_id": review.id, "kind": review.kind, "source": review.source}
    db.session.delete(review)
    db.session.commit()
    emit_event("review.deleted", data=data, workspace_id=workspace_id, user_id=current_user.id)
    return jsonify({"ok": True})


@bp.route("/api/reviews/<int:review_id>/findings", methods=["GET"])
@login_required
def api_review_findings(review_id: int):
    review = _get_review(review_id)
    query = ReviewFinding.query.filter_by(review_id=review.id)
    severity = request.args.get("severity")
    category = request.args.get("category")
    confidence = request.args.get("confidence")
    addressed = request.args.get("addressed")
    if severity:
        query = query.filter_by(severity=severity)
    if category:
        query = query.filter_by(category=category)
    if confidence:
        query = query.filter_by(confidence=confidence)
    if addressed in ("0", "1"):
        query = query.filter_by(addressed=addressed == "1")
    findings = query.order_by(ReviewFinding.severity.asc(), ReviewFinding.id).all()
    return jsonify([f.to_dict() for f in findings])


@bp.route("/api/reviews/findings/<int:finding_id>", methods=["PATCH"])
@login_required
def api_update_finding(finding_id: int):
    finding = (
        ReviewFinding.query.join(Review, ReviewFinding.review_id == Review.id)
        .filter(ReviewFinding.id == finding_id, Review.user_id == current_user.id)
        .first_or_404()
    )
    data = request.get_json(silent=True) or {}
    if "addressed" in data:
        finding.addressed = bool(data["addressed"])
    db.session.commit()
    return jsonify(finding.to_dict())


# --------------------------------------------------------------------------
# API: review configuration
# --------------------------------------------------------------------------


@bp.route("/api/projects/<int:project_id>/config", methods=["GET"])
@login_required
def api_get_config(project_id: int):
    project = _get_project(project_id)
    row = ReviewConfig.query.filter_by(user_id=current_user.id, project_id=project.id).first()
    return jsonify(_config_payload(row))


@bp.route("/api/projects/<int:project_id>/config", methods=["PATCH"])
@login_required
def api_update_config(project_id: int):
    project = _get_project(project_id)
    data = request.get_json(silent=True) or {}
    row = ReviewConfig.query.filter_by(user_id=current_user.id, project_id=project.id).first()
    if row is None:
        row = ReviewConfig(user_id=current_user.id, project_id=project.id)
        db.session.add(row)
    error = _apply_config(row, data)
    if error:
        db.session.rollback()
        return jsonify({"error": error}), 400
    db.session.commit()
    return jsonify(_config_payload(row))


# --------------------------------------------------------------------------
# API: quality metrics
# --------------------------------------------------------------------------


@bp.route("/api/metrics", methods=["GET"])
@login_required
def api_metrics():
    """Aggregate quality metrics per workspace, project, or the current user."""
    workspace_id = request.args.get("workspace_id", type=int)
    if workspace_id:
        workspace = Workspace.query.filter_by(
            id=workspace_id, user_id=current_user.id
        ).first_or_404()
        return jsonify(metrics_service.workspace_metrics(workspace))
    project_id = request.args.get("project_id", type=int)
    if project_id:
        project = _get_project(project_id)
        return jsonify(metrics_service.project_metrics(project))
    return jsonify(metrics_service.user_metrics(current_user))
