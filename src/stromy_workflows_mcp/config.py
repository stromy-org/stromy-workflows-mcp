"""Project settings for Stromy Workflows MCP.

INDIVIDUALITY — this file is yours. It is seeded once by the template and never
overwritten by `copier update`. Add domain-specific settings as fields on the
`Settings` subclass; they live alongside (and inherit) the framework fields from
the chrome `SettingsBase`. Never read `os.environ` directly inside components —
add a field here and read `settings.<field>`.
"""

from .config_base import SettingsBase


class Settings(SettingsBase):
    # Add project settings here, e.g.:
    #     example_api_url: str = ""
    pass


settings = Settings()
