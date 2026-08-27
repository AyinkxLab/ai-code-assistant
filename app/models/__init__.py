"""Database models.

Each model is imported here so that ``flask db migrate`` (via Flask-Migrate)
can discover every table in the application.
"""

from app.models.activity_event import ActivityEvent
from app.models.conversation import Conversation
from app.models.github_account import GithubAccount
from app.models.invitation import WorkspaceInvitation
from app.models.message import Message
from app.models.notification import Notification
from app.models.notification_preference import NotificationPreference
from app.models.plugin import CapabilityGrant, Plugin, PluginInstallation
from app.models.project import Project
from app.models.project_comment import ProjectComment
from app.models.project_file import ProjectFile
from app.models.project_message import ProjectMessage
from app.models.prompt import Prompt
from app.models.review import Review
from app.models.review_config import ReviewConfig
from app.models.review_finding import ReviewFinding
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.models.workspace_settings import WorkspaceSettings

__all__ = [
    "ActivityEvent",
    "CapabilityGrant",
    "Conversation",
    "GithubAccount",
    "Message",
    "Notification",
    "NotificationPreference",
    "Plugin",
    "PluginInstallation",
    "Project",
    "ProjectComment",
    "ProjectFile",
    "ProjectMessage",
    "Prompt",
    "Review",
    "ReviewConfig",
    "ReviewFinding",
    "User",
    "Workspace",
    "WorkspaceInvitation",
    "WorkspaceMember",
    "WorkspaceSettings",
]
