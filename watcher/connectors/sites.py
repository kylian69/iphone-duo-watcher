"""Revendeurs et opérateurs. Ajouter un site = une classe ici (ou dans un nouveau module
du paquet) + son bloc dans config.yaml + les URL produit des modèles."""

from __future__ import annotations

from .base import register
from .retail import RetailConnector


@register("orange")
class OrangeConnector(RetailConnector):
    label = "Orange"
    is_operator = True


@register("bouygues")
class BouyguesConnector(RetailConnector):
    label = "Bouygues Telecom"
    is_operator = True


@register("sfr")
class SfrConnector(RetailConnector):
    label = "SFR"
    is_operator = True


@register("boulanger")
class BoulangerConnector(RetailConnector):
    label = "Boulanger"


@register("fnac")
class FnacConnector(RetailConnector):
    label = "Fnac"


@register("darty")
class DartyConnector(RetailConnector):
    label = "Darty"


@register("cdiscount")
class CdiscountConnector(RetailConnector):
    label = "Cdiscount"


@register("amazon")
class AmazonConnector(RetailConnector):
    label = "Amazon.fr"
    unavailable_patterns = RetailConnector.unavailable_patterns + [r"actuellement indisponible"]
