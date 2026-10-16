import json
import os
from dataclasses import dataclass
from pathlib import Path

import keyring
from keyring.errors import KeyringError

DEFAULT_API_URL = "http://localhost:8000"
KEYRING_SERVICE = "rastro"
TOKEN_ENV_VAR = "RASTRO_TOKEN"


@dataclass(frozen=True)
class Config:
    api_url: str


def data_dir(root: Path) -> Path:
    return root / ".rastro"


def pending_dir(root: Path) -> Path:
    return data_dir(root) / "pending"


def rejected_dir(root: Path) -> Path:
    return data_dir(root) / "rejected"


def config_path(root: Path) -> Path:
    return data_dir(root) / "config.json"


def load_config(root: Path) -> Config | None:
    try:
        raw = json.loads(config_path(root).read_text(encoding="utf-8"))
        return Config(api_url=str(raw["api_url"]).rstrip("/"))
    except (OSError, ValueError, KeyError):
        return None


def save_config(root: Path, config: Config) -> None:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"api_url": config.api_url}, indent=2) + "\n", encoding="utf-8")


def _keyring_user(root: Path) -> str:
    return str(root.resolve())


def save_token(root: Path, token: str) -> bool:
    """Store the token in the OS keychain. Returns False if no keychain is available (the
    token is never written to .rastro/config.json)."""
    try:
        keyring.set_password(KEYRING_SERVICE, _keyring_user(root), token)
    except KeyringError:
        return False
    return True


def load_token(root: Path) -> str | None:
    """RASTRO_TOKEN (CI, headless machines) takes precedence over the keychain."""
    env = os.environ.get(TOKEN_ENV_VAR)
    if env:
        return env
    try:
        return keyring.get_password(KEYRING_SERVICE, _keyring_user(root))
    except KeyringError:
        return None
