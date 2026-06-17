"""Application configuration loaded from environment variables."""

from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

load_dotenv(override=True)


class Settings(BaseSettings):
    # Anthropic
    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    default_model: str = Field("claude-haiku-4-5-20251001", alias="DEFAULT_MODEL")

    # Jira
    jira_url: str = Field("", alias="JIRA_URL")
    jira_email: str = Field("", alias="JIRA_EMAIL")
    jira_token: str = Field("", alias="JIRA_TOKEN")

    # HubSpot
    hubspot_token: str = Field("", alias="HUBSPOT_TOKEN")

    # Xero
    xero_client_id: str = Field("", alias="XERO_CLIENT_ID")
    xero_client_secret: str = Field("", alias="XERO_CLIENT_SECRET")
    xero_tenant_id: str = Field("", alias="XERO_TENANT_ID")
    # Short-lived OAuth2 access token — obtain via Xero OAuth2 PKCE flow and paste here.
    # Expires after 30 minutes; refresh using xero_client_id + xero_client_secret + refresh token.
    xero_access_token: str = Field("", alias="XERO_ACCESS_TOKEN")

    # Harvest
    harvest_account_id: str = Field("", alias="HARVEST_ACCOUNT_ID")
    harvest_token: str = Field("", alias="HARVEST_TOKEN")

    # PandaDoc
    pandadoc_api_key: str = Field("", alias="PANDADOC_API_KEY")

    # App
    mock_mode: bool = Field(True, alias="MOCK_MODE")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    cache_ttl_seconds: int = Field(300, alias="CACHE_TTL_SECONDS")
    # Model selection — orchestrator needs Sonnet-level capability for reliable tool use;
    # sub-agents (interpreter, fuzzy-match, reporter) can use the cheaper default model.
    orchestrator_model: str = Field("claude-sonnet-4-6", alias="ORCHESTRATOR_MODEL")
    claude_timeout_seconds: int = Field(90, alias="CLAUDE_TIMEOUT_SECONDS")

    model_config = {"populate_by_name": True}


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
