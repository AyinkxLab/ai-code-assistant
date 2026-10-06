"""In-process background workers for project imports.

Imports are offloaded to a bounded ``ThreadPoolExecutor`` so the HTTP request
returns immediately with the project in ``indexing`` status; the client polls
``GET /workspaces/api/workspaces/<id>/projects`` for the ``progress`` percentage
and terminal ``status``/``error_message``.

No new external dependencies: the worker pushes its own Flask app context and
reuses the existing importing services. Tests can swap the executor via
:func:`set_executor` (e.g. to a fake that records submissions) and run jobs
deterministically with :func:`run_import_job`.
"""

from __future__ import annotations

import logging
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import UTC, datetime

from app.extensions import db
from app.models import Project, User
from app.models.project import STATUS_FAILED, STATUS_INDEXING, STATUS_READY

logger = logging.getLogger(__name__)

_executor: Executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="import-job")


def set_executor(executor: Executor) -> None:
    """Replace the module-level executor (used by tests)."""
    global _executor
    _executor = executor


def get_executor() -> Executor:
    return _executor


def _set_progress(project_id: int, progress: int) -> None:
    project = db.session.get(Project, project_id)
    if project is None:
        return
    project.progress = max(0, min(int(progress), 100))
    db.session.commit()


def run_import_job(app, project_id: int, task) -> None:
    """Run ``task(project, user, set_progress)`` and finalize the project.

    On success the project becomes ``ready`` at 100%. On any failure it becomes
    ``failed`` with the error message stored (never raised to the request)."""
    with app.app_context():
        try:
            project = db.session.get(Project, project_id)
            if project is None:
                return
            user = db.session.get(User, project.user_id)
            task(project, user, lambda pct: _set_progress(project_id, pct))

            project = db.session.get(Project, project_id)
            project.status = STATUS_READY
            project.progress = 100
            project.error_message = None
            project.indexed_at = datetime.now(UTC)
            db.session.commit()
        except Exception as exc:
            logger.exception("Import job failed for project %s", project_id)
            db.session.rollback()
            project = db.session.get(Project, project_id)
            if project is not None:
                project.status = STATUS_FAILED
                project.error_message = str(exc)[:2000]
                project.progress = 0
                db.session.commit()


def submit_import_job(app, project_id: int, task) -> None:
    """Queue an import job (or run it inline when async is disabled)."""
    if app.config.get("IMPORT_JOBS_ASYNC", True):
        _executor.submit(run_import_job, app, project_id, task)
    else:
        run_import_job(app, project_id, task)


__all__ = [
    "STATUS_INDEXING",
    "get_executor",
    "run_import_job",
    "set_executor",
    "submit_import_job",
]
