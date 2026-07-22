"""Project settings for Stromy Workflows MCP.

INDIVIDUALITY — this file is yours. It is seeded once by the template and never
overwritten by `copier update`. Add domain-specific settings as fields on the
`Settings` subclass; they live alongside (and inherit) the framework fields from
the chrome `SettingsBase`. Never read `os.environ` directly inside components —
add a field here and read `settings.<field>`.
"""

from .config_base import SettingsBase


class Settings(SettingsBase):
    fs_roots: list[str] = ["skills"]
    stromy_pg_dsn: str = ""
    contracts_dir: str = "components/resources/contracts"
    client_role_prefix: str = "client."
    operator_role: str = "operator"
    client_scope_map: str = ""

    azure_subscription_id: str = ""
    azure_resource_group: str = "rg-Stromy"
    azure_runner_job: str = "stromy-runner"
    azure_api_version: str = "2025-01-01"
    azure_management_scope: str = "https://management.azure.com/.default"


settings = Settings()
