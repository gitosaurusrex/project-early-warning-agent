"""Build live connectors from settings. Tokens come only from the environment / .env."""

from pewa.config import Settings
from pewa.connectors.base import Connector
from pewa.connectors.github import GitHubConnector
from pewa.connectors.jira import JiraConnector
from pewa.connectors.slack import SlackConnector
from pewa.schemas import WORKFLOW


def make_connector(source: str, settings: Settings) -> Connector:
    """A connector for `source`; callers check `settings.has_credentials(source)` first."""
    if source == "jira":
        unknown = sorted(set(settings.status_map().values()) - set(WORKFLOW))
        if unknown:
            raise ValueError(f"JIRA_STATUS_MAP maps to unknown statuses {unknown}; use {', '.join(WORKFLOW)}")
        return JiraConnector(
            settings.jira_base_url or "", settings.jira_email or "",
            settings.jira_api_token.get_secret_value() if settings.jira_api_token else "",
            settings.jira_project_key or "", points_field=settings.jira_points_field,
            epic_link_field=settings.jira_epic_link_field, status_map=settings.status_map(),
        )
    if source == "github":
        return GitHubConnector(settings.github_token.get_secret_value() if settings.github_token else "",
                               settings.github_repos, api_url=settings.github_api_url)
    if source == "slack":
        return SlackConnector(settings.slack_bot_token.get_secret_value() if settings.slack_bot_token else "",
                              settings.slack_channels, thread_lookback_days=settings.slack_thread_lookback_days)
    raise ValueError(f"unknown source: {source}")
