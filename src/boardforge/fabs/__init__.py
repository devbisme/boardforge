"""Fab adapters: ``get(name, tier)`` imports ``boardforge.fabs.<name>.Adapter``."""

import importlib
import os


def available():
    here = os.path.dirname(__file__)
    return sorted(d for d in os.listdir(here)
                  if os.path.exists(os.path.join(here, d, "profile.toml")))


def get(name, tier=None):
    try:
        mod = importlib.import_module(f"boardforge.fabs.{name}")
    except ModuleNotFoundError:
        raise ValueError(f"unknown fab {name!r}; known: {available()}") from None
    return mod.Adapter(tier)
