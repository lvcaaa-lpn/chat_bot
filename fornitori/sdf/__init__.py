"""Fornitore SDF (SAME / Deutz-Fahr / Lamborghini): portale live."""

from .adapter import FornitoreSdf


def crea():
    try:
        return FornitoreSdf()
    except Exception:
        return None