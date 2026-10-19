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


class RecoverySettings(BaseSettings):
    """Recovery of jobs orphaned by a worker that died (crash, OOM kill, restart, reboot)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # Each worker writes a heartbeat this often.
    worker_heartbeat_interval_seconds: float = 10.0
    # A worker silent for this long is considered dead and its `doing` jobs are put back. The
    # heartbeat runs on the worker's event loop, so a long native call (parsing a huge repository)
    # delays it; the timeout must be several times the longest such stall. A normal `analyze_pr`
    # takes ~30 s and a model call seconds, so 120 s is safe and still frees a repository in
    # about two minutes after a crash.
    worker_stalled_timeout_seconds: float = 120.0
    # Total runs a job gets before recovery gives up on it (a job that keeps killing its worker).
    job_max_attempts: int = 5
