"""Runtime configuration, read from COLLAB_* environment variables."""

from __future__ import annotations

import json
from functools import cached_property
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["reader", "writer"]


class PrincipalGrant(BaseModel):
    """What one verified identity may do. Keyed by the token's subject claim."""

    alias: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    workspace: str = Field(default="main", pattern=r"^[a-z][a-z0-9]{1,15}$")
    role: Role = "writer"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COLLAB_", extra="ignore")

    # --- storage -----------------------------------------------------------
    # Production: account name + managed identity. Local tests: connection string
    # for the Azurite emulator. Never set a real account key here.
    storage_account: str | None = None
    storage_connection_string: str | None = None
    managed_identity_client_id: str | None = None
    table_name: str = Field(default="hub", pattern=r"^[A-Za-z][A-Za-z0-9]{2,62}$")
    blob_container: str = Field(default="content", pattern=r"^[a-z0-9](-?[a-z0-9]){2,62}$")

    # --- auth (always on; there is no anonymous mode) ------------------------
    tenant_id: str | None = None  # Entra: derives issuer + JWKS when those are unset
    oidc_issuer: str | None = None
    oidc_jwks_url: str | None = None
    oidc_jwks_json: str | None = None  # tests only: static JWKS document
    oidc_audience: str  # Entra v2: the API app's client ID (GUID)
    subject_claim: str = "oid"
    scope_read: str = "Collab.Read"
    scope_write: str = "Collab.ReadWrite"
    authorization_servers: str | None = None  # advertised in RFC 9728 metadata
    principals: dict[str, PrincipalGrant] = Field(default_factory=dict)

    # --- OAuth authorization server for ChatGPT / Claude.ai connectors --------
    # The hub becomes an OAuth AS (DCR + PKCE) whose user sign-in is delegated to
    # Entra ID through a separate "login" app registration. Entra JWTs (Claude
    # Code / Codex via az) keep working alongside the hub's own opaque tokens.
    oauth_enabled: bool = False
    oauth_login_client_id: str | None = None  # Entra app used for user sign-in
    oauth_redirect_allowlist: str = (
        "https://claude.ai/api/mcp/auth_callback,"
        "https://claude.com/api/mcp/auth_callback,"
        "https://chatgpt.com/connector_platform_oauth_redirect,"
        "https://chatgpt.com/connector/oauth/*,"
        "http://localhost:*,http://127.0.0.1:*"
    )
    oauth_access_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    oauth_refresh_ttl_seconds: int = Field(default=30 * 86400, ge=3600, le=90 * 86400)
    oauth_max_clients: int = Field(default=200, ge=1, le=10000)

    # --- http --------------------------------------------------------------
    public_url: str = "http://127.0.0.1:8000"
    allowed_hosts: str = ""  # comma separated; defaults to the public_url host
    max_body_bytes: int = Field(default=256 * 1024, ge=4096, le=4 * 1024 * 1024)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("principals", mode="before")
    @classmethod
    def _parse_principals(cls, v: object) -> object:
        if isinstance(v, str):
            return json.loads(v) if v.strip() else {}
        return v

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if not (self.storage_account or self.storage_connection_string):
            raise ValueError("set COLLAB_STORAGE_ACCOUNT or COLLAB_STORAGE_CONNECTION_STRING")
        if not self.issuer:
            raise ValueError("set COLLAB_TENANT_ID or COLLAB_OIDC_ISSUER")
        if not (self.oidc_jwks_json or self.jwks_url):
            raise ValueError("set COLLAB_TENANT_ID or COLLAB_OIDC_JWKS_URL")
        if not self.principals:
            raise ValueError("COLLAB_PRINCIPALS is empty: nobody could use the server")
        if self.oauth_enabled and not self.oauth_login_client_id:
            raise ValueError("COLLAB_OAUTH_ENABLED needs COLLAB_OAUTH_LOGIN_CLIENT_ID")
        return self

    @property
    def redirect_allowlist(self) -> list[str]:
        return [p.strip() for p in self.oauth_redirect_allowlist.split(",") if p.strip()]

    @property
    def issuer(self) -> str | None:
        if self.oidc_issuer:
            return self.oidc_issuer
        if self.tenant_id:
            return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"
        return None

    @property
    def jwks_url(self) -> str | None:
        if self.oidc_jwks_url:
            return self.oidc_jwks_url
        if self.tenant_id:
            return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"
        return None

    @property
    def resource_url(self) -> str:
        return self.public_url.rstrip("/") + "/mcp"

    @cached_property
    def host_allowlist(self) -> list[str]:
        if self.allowed_hosts.strip():
            return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]
        from urllib.parse import urlparse

        netloc = urlparse(self.public_url).netloc
        return [netloc]
