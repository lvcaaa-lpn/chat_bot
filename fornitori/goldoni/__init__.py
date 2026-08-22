"""Fornitore Goldoni: catalogo HTML statico scaricato in locale."""

from .adapter import FornitoreGoldoni


def crea():
    """Chiamata dal registro all'avvio. Se qualcosa manca, restituisce None."""
    try:
        return FornitoreGoldoni()
    except Exception:
        return None
