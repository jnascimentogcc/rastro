from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class GitHubSettings(BaseSettings):
    """GitHub App credentials, read from the environment (or `.env`)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    github_app_id: int | None = None
    github_app_private_key: SecretStr | None = None  # PEM; `\n` escapes are accepted
    github_app_private_key_path: Path | None = None
    github_webhook_secret: SecretStr | None = None
    github_api_url: str = "https://api.github.com"

    def private_key(self) -> str:
        if self.github_app_private_key is not None:
            return self.github_app_private_key.get_secret_value().replace("\\n", "\n")
        if self.github_app_private_key_path is not None:
            return self.github_app_private_key_path.read_text(encoding="utf-8")
        raise RuntimeError("Set GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH")

    def require_app_id(self) -> int:
        if self.github_app_id is None:
            raise RuntimeError("Set GITHUB_APP_ID")
        return self.github_app_id

    def webhook_secret(self) -> str:
        if self.github_webhook_secret is None:
            raise RuntimeError("Set GITHUB_WEBHOOK_SECRET")
        return self.github_webhook_secret.get_secret_value()
