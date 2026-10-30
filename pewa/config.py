"""Settings loaded from environment variables and an optional .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    anthropic_api_key: SecretStr | None = None
    # Same key under a name hosted environments won't reserve (e.g. Claude Code cloud sessions,
    # which keep ANTHROPIC_API_KEY for their own use). Takes precedence when both are set.
    pewa_anthropic_api_key: SecretStr | None = None
    pewa_model: str = "claude-sonnet-5-5"

    # Storage / project
    pewa_db_path: Path = Path("pewa.db")
    pewa_project: str = "atlas"
    pewa_fixtures_dir: Path = REPO_ROOT / "data" / "fixtures"

    # Jira
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: SecretStr | None = None
    jira_project_key: str | None = None

    # GitHub
    github_token: SecretStr | None = None
    github_repos: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Slack
    slack_bot_token: SecretStr | None = None
    slack_channels: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Signal thresholds (used from Phase 2 on)
    pewa_stale_review_days: int = 3
    pewa_stale_ticket_days: int = 5
    pewa_scope_growth_pct: float = 20.0
    pewa_ci_flaky_failures: int = 2
    pewa_owner_overload_n: int = 3
    pewa_burn_window_weeks: int = 4
    # Only recent reopen / CI / Slack activity counts as a live signal.
    pewa_signal_lookback_days: int = 14
    pewa_blocker_phrases: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "blocked on", "blocked by", "waiting on", "waiting for", "can't proceed", "cannot proceed",
            "stuck on", "stuck behind", "held up", "no movement",
        ]
    )

    @field_validator("github_repos", "slack_channels", "pewa_blocker_phrases", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    def anthropic_key(self) -> str | None:
        key = self.pewa_anthropic_api_key or self.anthropic_api_key
        return key.get_secret_value() if key else None

    def has_credentials(self, source: str) -> bool:
        if source == "jira":
            return all([self.jira_base_url, self.jira_email, self.jira_api_token, self.jira_project_key])
        if source == "github":
            return bool(self.github_token and self.github_repos)
        if source == "slack":
            return bool(self.slack_bot_token and self.slack_channels)
        raise ValueError(f"unknown source: {source}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
