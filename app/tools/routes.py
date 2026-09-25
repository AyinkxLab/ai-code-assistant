"""AI tool routes: code generation, code actions, and file analysis.

All actions build a task-specific system prompt and delegate to the configured
LLM provider. File analysis reads plain-text uploads and has the model explain,
refactor, review, or comment on them.
"""

from pathlib import Path

from flask import jsonify, request
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import Conversation, Message
from app.services.llm import LLMProviderError, get_provider
from app.services.soroban_generation import generate_soroban_skeleton
from app.tools import bp

ALLOWED_EXTENSIONS = {
    "py",
    "js",
    "ts",
    "jsx",
    "tsx",
    "html",
    "css",
    "sql",
    "java",
    "go",
    "rs",
    "rb",
    "php",
    "c",
    "cpp",
    "h",
    "hpp",
    "cs",
    "sh",
    "json",
    "yaml",
    "yml",
    "toml",
    "md",
    "txt",
}

ACTION_PROMPTS = {
    "generate": (
        "You are an expert software engineer. Write production-quality {language} "
        "code for the following request. Return only the code inside a single code block."
    ),
    "explain": (
        "You are a senior developer explaining code to a peer. Explain the following "
        "code step by step: what it does, how it works, and any notable details."
    ),
    "refactor": (
        "You are a senior developer. Refactor the following code to improve readability, "
        "maintainability, and performance while preserving behavior. Show the improved "
        "code and briefly summarize the changes."
    ),
    "bugs": (
        "You are a code reviewer. Find bugs, edge cases, and security issues in the "
        "following code. List each issue with severity, the relevant snippet, and a fix."
    ),
    "optimize": (
        "You are a performance engineer. Suggest concrete optimizations for the following "
        "code with before/after examples where helpful."
    ),
    "comments": (
        "You are a documentation specialist. Add clear, concise comments and docstrings "
        "to the following code and return the fully commented version."
    ),
    "docs": (
        "You are a technical writer. Write comprehensive documentation (overview, setup, "
        "usage, API reference) for the following code."
    ),
    "commit": (
        "You are a git expert. Write a concise, conventional commit message for the "
        "following diff or change description. Output only the commit message."
    ),
}


def _is_allowed(filename: str) -> bool:
    return Path(filename).suffix.lstrip(".").lower() in ALLOWED_EXTENSIONS


def _read_upload(field: str = "file") -> tuple[str, str] | tuple[None, str]:
    """Extract and validate an uploaded source file.

    Returns ``(filename, text)`` on success or ``(None, error)`` on failure.
    """
    file = request.files.get(field)
    if file is None or not file.filename:
        return None, "No file was uploaded."
    if not _is_allowed(file.filename):
        return None, "Unsupported file type."
    try:
        raw = file.read()
    except OSError as exc:
        return None, f"Could not read the uploaded file: {exc}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "File must be UTF-8 encoded text."
    if len(text) > 200_000:
        return None, "File is too large to analyze (max 200,000 characters)."
    return secure_filename(file.filename), text


def _run_action(action: str, prompt: str) -> str:
    """Run a single AI action and return the model output."""
    try:
        provider = get_provider()
        return provider.complete(
            [
                {"role": "system", "content": "You are a helpful AI coding assistant."},
                {"role": "user", "content": prompt},
            ]
        )
    except LLMProviderError as exc:
        return f"[provider error] {exc}"


@bp.route("/generate", methods=["POST"])
@login_required
def generate():
    """Generate code from a natural-language request."""
    data = request.get_json(silent=True) or {}
    description = (data.get("description") or "").strip()
    language = (data.get("language") or "python").strip() or "python"
    if not description:
        return jsonify({"error": "A description is required."}), 400

    system = ACTION_PROMPTS["generate"].format(language=language)
    result = _run_action("generate", f"{system}\n\nRequest: {description}")
    return jsonify({"result": result, "language": language})


@bp.route("/code", methods=["POST"])
@login_required
def code_action():
    """Run a code action (explain/refactor/bugs/optimize/comments/docs/commit)."""
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip().lower()
    code = (data.get("code") or "").strip()
    if action not in ACTION_PROMPTS or action == "generate":
        return jsonify({"error": "Unsupported action."}), 400
    if not code:
        return jsonify({"error": "Code or diff content is required."}), 400

    system = ACTION_PROMPTS[action]
    result = _run_action(action, f"{system}\n\nCode:\n{code}")
    return jsonify({"action": action, "result": result})


@bp.route("/analyze", methods=["POST"])
@login_required
def analyze_file():
    """Upload a source file and run an AI analysis over its contents.

    Accepts ``multipart/form-data`` with a ``file`` field and an optional
    ``action`` field (default ``explain``).
    """
    filename, text = _read_upload()
    if filename is None:
        return jsonify({"error": text}), 400

    action = (request.form.get("action") or "explain").strip().lower()
    if action not in ACTION_PROMPTS or action == "generate":
        action = "explain"

    system = ACTION_PROMPTS[action]
    result = _run_action(action, f"{system}\n\nFile: {filename}\n\nCode:\n{text}")
    return jsonify({"filename": filename, "action": action, "result": result})


@bp.route("/soroban/skeleton", methods=["POST"])
@login_required
def soroban_skeleton():
    """Generate a labeled Soroban contract skeleton from a description (#187).

    The model output is post-processed so the result always has a valid
    ``#[contractimpl]`` structure and the ``soroban-sdk`` dependency. It is
    explicitly AI-generated and has not been compiled or verified.
    """
    data = request.get_json(silent=True) or {}
    description = (data.get("description") or "").strip()
    if not description:
        return jsonify({"error": "A description is required."}), 400
    name = (data.get("name") or "").strip() or None
    return jsonify(generate_soroban_skeleton(description, name=name))


@bp.route("/send-to-chat", methods=["POST"])
@login_required
def send_to_chat():
    """Create a conversation prefilled with an AI tool result.

    Used to move generated code or analysis output into the chat UI for
    follow-up questions.
    """
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Content is required."}), 400

    conversation = Conversation(user_id=current_user.id, title=content[:60] or "New conversation")
    conversation.messages.append(Message(role="user", content=content))
    db.session.add(conversation)
    db.session.commit()
    return jsonify({"conversation_id": conversation.id}), 201
