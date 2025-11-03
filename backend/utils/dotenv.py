"""Optional loader for python-dotenv with graceful fallback."""

from __future__ import annotations

import importlib
from typing import Any


def load_dotenv(*args: Any, **kwargs: Any) -> Any:
    """
    Attempt to import python-dotenv on demand and invoke load_dotenv.

    When the dependency is unavailable, the call becomes a no-op so that
    higher-level modules can continue importing until the package is installed.
    """
    try:
        module = importlib.import_module("dotenv")
    except ModuleNotFoundError:
        return None
    loader = getattr(module, "load_dotenv", None)
    if callable(loader):
        return loader(*args, **kwargs)
    return None


__all__ = ["load_dotenv"]

