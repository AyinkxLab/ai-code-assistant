"""Database models.

Each model is imported here so that ``flask db migrate`` (via Flask-Migrate)
can discover every table in the application.
"""

from app.models.activity_event import ActivityEvent
from app.models.analyzed_file import AnalyzedFile
from app.models.api_key import ApiKey
from app.models.audit_log import AuditLog
from app.models.budget import Budget
from app.models.conversation import Conversation
from app.models.conversation_share import ConversationShare
from app.models.file_analysis import FileAnalysis
from app.models.github_account import GithubAccount
from app.models.integration_credential import IntegrationCredential
from app.models.invitation import WorkspaceInvitation
from app.models.job import Job
from app.models.message import Message
from app.models.message_attachment import MessageAttachment
from app.models.notification import Notification
from app.models.notification_preference import NotificationPreference
from app.models.plugin import CapabilityGrant, Plugin, PluginInstallation, PluginLockEntry
from app.models.plugin_collection import PluginCollection
from app.models.plugin_error_report import PluginErrorReport
from app.models.plugin_install_event import PluginInstallEvent
from app.models.plugin_review import PluginReview
from app.models.project import Project
from app.models.project_chat_session import ProjectChatSession
from app.models.project_comment import ProjectComment
from app.models.project_file import ProjectFile
from app.models.project_message import ProjectMessage
from app.models.prompt import Prompt
from app.models.prompt_version import PromptVersion
from app.models.provider_billing_record import ProviderBillingRecord
from app.models.review import Review
from app.models.review_comment import ReviewComment
from app.models.review_config import ReviewConfig
from app.models.review_finding import ReviewFinding
from app.models.stellar_security_finding import StellarSecurityFinding
from app.models.usage_record import UsageRecord
from app.models.user import User
from app.models.webhook import WebhookDelivery, WebhookSubscription
from app.models.workspace import Workspace
from app.models.workspace_budget_override import WorkspaceBudgetOverride
from app.models.workspace_member import WorkspaceMember
from app.models.workspace_price_override import PriceOverride, WorkspacePriceOverride
from app.models.workspace_settings import WorkspaceSettings

__all__ = [
    "ActivityEvent",
    "AnalyzedFile",
    "ApiKey",
    "AuditLog",
    "Budget",
    "CapabilityGrant",
    "Conversation",
    "ConversationShare",
    "FileAnalysis",
    "GithubAccount",
    "IntegrationCredential",
    "Job",
    "Message",
    "MessageAttachment",
    "Notification",
    "NotificationPreference",
    "Plugin",
    "PluginCollection",
    "PluginErrorReport",
    "PluginInstallEvent",
    "PluginInstallation",
    "PluginLockEntry",
    "PluginReview",
    "PriceOverride",
    "Project",
    "ProjectChatSession",
    "ProjectComment",
    "ProjectFile",
    "ProjectMessage",
    "Prompt",
    "PromptVersion",
    "ProviderBillingRecord",
    "Review",
    "ReviewComment",
    "ReviewConfig",
    "ReviewFinding",
    "StellarSecurityFinding",
    "UsageRecord",
    "User",
    "WebhookDelivery",
    "WebhookSubscription",
    "Workspace",
    "WorkspaceBudgetOverride",
    "WorkspaceInvitation",
    "WorkspaceMember",
    "WorkspacePriceOverride",
    "WorkspaceSettings",
]
