from datetime import date

from watcher.dates import parse_delay, parse_price

TODAY = date(2026, 10, 23)


def test_relative():
    assert parse_delay("Disponible aujourd’hui", TODAY) == 0
    assert parse_delay("Retrait demain", TODAY) == 1
    assert parse_delay("Livré sous 3 jours", TODAY) == 3
    assert parse_delay("Expédié sous 24h", TODAY) == 1
    assert parse_delay("Expédition : 4–5 semaines", TODAY) == 28


def test_absolute():
    assert parse_delay("Livraison : 29/10", TODAY) == 6
    assert parse_delay("Livraison le jeu. 12 nov.", TODAY) == 20
    assert parse_delay("Livraison : 25/01/2027 – 01/02/2027", TODAY) == 94
    assert parse_delay("Livraison à partir du 16 novembre", TODAY) == 24
    assert parse_delay("livré le 5 janvier", TODAY) == 74  # année suivante


def test_none():
    assert parse_delay("Livraison gratuite", TODAY) is None
    assert parse_delay(None, TODAY) is None


def test_price():
    assert parse_price("2 339,00 €") == 2339.0
    assert parse_price("2339.00") == 2339.0
    assert parse_price(1469) == 1469.0
