"""Config loading. Everything tunable lives in YAML so you never edit code."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def _load(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_sources() -> dict[str, Any]:
    return _load("sources.yaml")


def load_weights() -> dict[str, float]:
    data = _load("weights.yaml")
    return {k: float(v) for k, v in (data.get("weights") or {}).items()}


def load_lexicons() -> dict[str, Any]:
    return _load("lexicons.yaml")


def env(key: str, default: str | None = None) -> str | None:
    """Read a secret from the environment, falling back to a local .env file."""
    if key in os.environ:
        return os.environ[key]
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    return default


def has(key: str) -> bool:
    value = env(key)
    return bool(value and not value.startswith("your_"))
