from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    """Model configuration from the environment (or `.env`). Models have no default on purpose:
    which one to use is decided by evaluating against `fixtures/clinica-demo`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    openai_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_model_impact: str | None = None  # optional, defaults to llm_model
    embedding_model: str | None = None
    embedding_dimensions: int = 1024  # must match vector(1024) in the database

    # Optional prices (USD per million tokens) so calls get a cost estimate. Unset = unknown.
    llm_price_input_per_mtok: float | None = None
    llm_price_output_per_mtok: float | None = None
    embedding_price_per_mtok: float | None = None

    # Tokens (input + output, embeddings included) one repository may use per calendar month
    # (UTC). Once reached, extraction and impact analysis are skipped until the next month.
    # Unset or 0 = unlimited.
    llm_monthly_token_budget: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.openai_api_key and self.llm_model and self.embedding_model)

    def impact_model(self) -> str | None:
        return self.llm_model_impact or self.llm_model
