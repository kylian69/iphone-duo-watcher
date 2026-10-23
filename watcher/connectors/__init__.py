"""Découverte automatique des connecteurs : tout module de ce paquet est importé,
et chaque classe décorée par @register y devient disponible."""

import importlib
import pkgutil

from .base import REGISTRY, BlockedError, Connector, ConnectorError, FormatError, register

for _mod in pkgutil.iter_modules(__path__):
    if _mod.name != "base":
        importlib.import_module(f"{__name__}.{_mod.name}")

__all__ = ["REGISTRY", "BlockedError", "Connector", "ConnectorError", "FormatError", "register"]
