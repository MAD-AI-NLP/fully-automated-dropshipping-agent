"""
config_loader.py
=================
Single source of truth for pipeline-level settings (as opposed to secrets,
which stay in .env — API keys, tokens, etc. never belong in config.json).

Layout:
    <repo root>/
        config.json        <- you edit this
        config_loader.py    <- this file
        layer1/...
        layer2/...

Usage (from anywhere in either layer):

    from config_loader import get_layer_config
    cfg = get_layer_config("layer1")   # -> dict, {} if section missing
    cfg = get_layer_config("layer2")

Resolution order for the file path itself:
    1. PIPELINE_CONFIG_PATH env var, if set
    2. config.json next to this file (repo root)

The file is read once and cached; call load_config(force_reload=True) if
you edit config.json mid-process (e.g. in a long-running service) and want
the change picked up without restarting.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
_config_cache: Optional[Dict[str, Any]] = None


def _config_path() -> Path:
    override = os.getenv("PIPELINE_CONFIG_PATH")
    return Path(override) if override else _DEFAULT_CONFIG_PATH


def load_config(force_reload: bool = False) -> Dict[str, Any]:
    """Load (and cache) the full config.json as a dict."""
    global _config_cache
    if _config_cache is None or force_reload:
        path = _config_path()
        if not path.exists():
            raise FileNotFoundError(
                f"config.json not found at '{path}'. Create one at the project "
                f"root (see config.json for the expected 'layer1'/'layer2' shape), "
                f"or set PIPELINE_CONFIG_PATH to point somewhere else."
            )
        with open(path, "r") as f:
            _config_cache = json.load(f)
        print(f"[Config] ✅ Loaded pipeline config from {path}")
    return _config_cache


def get_layer_config(layer: str) -> Dict[str, Any]:
    """Return the sub-dict for a given layer (e.g. 'layer1', 'layer2').
    Returns {} if the section is missing, so callers can safely .get()
    off it with their own defaults."""
    cfg = load_config()
    section = cfg.get(layer)
    if section is None:
        print(f"[Config] ⚠️  No '{layer}' section in config.json — using code defaults.")
        return {}
    return section