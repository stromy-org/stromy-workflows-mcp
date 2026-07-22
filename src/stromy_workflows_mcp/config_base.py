"""Framework settings base (ORG-PLAN-086 §3).

CHROME — do not hand-edit. `copier update` overwrites this file freely. Add your
own settings to the `Settings` subclass in `config.py` (project-owned); it
inherits every field defined here.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsBase(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    fastmcp_transport: str = "http"
    fastmcp_host: str = "127.0.0.1"
    fastmcp_port: int = 8000
    mcp_dev_mode: bool = False
    log_level: str = "INFO"
    fs_roots: list[str] = ["skills"]

    # OAuth (Microsoft Entra ID). Durable sessions are provided by a persistent
    # Azure Files mount at FastMCP's default home path (ORG-PLAN-073) — FastMCP
    # derives the signing/encryption keys deterministically from the stable
    # client secret, so NO signing-key / Redis / encryption-key settings are
    # needed here. See auth.py.
    oauth_enable: bool = False
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_tenant_id: str = ""
    oauth_base_url: str = ""
    oauth_required_scopes: str = ""

