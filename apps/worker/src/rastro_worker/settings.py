from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class ImpactSettings(BaseSettings):
    """Pull request impact analysis knobs (spec section 7.7)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # A rule is only reported when the model is at least this confident. Irrelevant comments
    # kill adoption, so the default is deliberately high.
    impact_threshold: float = 0.7
    impact_comment_language: Literal["en", "pt"] = "en"
    impact_max_candidates: int = 10  # model calls per pull request (cost control)
    impact_concurrency: int = 4
