"""Local user settings — stores API key, provider, model to disk so users
don't have to set env vars every session."""
import json
import os
import sys
from pathlib import Path


def _config_dir() -> Path:
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "LuLuBot"
    else:
        d = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LuLuBot"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_path() -> Path:
    return _config_dir() / "settings.json"


def load() -> dict:
    try:
        return json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(data: dict) -> None:
    _config_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def get(key: str, default=None):
    return load().get(key, default)


def set_value(key: str, value) -> None:
    d = load()
    d[key] = value
    save(d)


def get_api_key() -> str | None:
    return get("api_key") or None
