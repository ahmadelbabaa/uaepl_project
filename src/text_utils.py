"""Shared name-normalization helpers used by both cleaning and cross-source matching."""
import re
import unicodedata

import pandas as pd


def fix_mojibake(s: str) -> str:
    """Repair UTF-8-bytes-decoded-as-single-byte-encoding mojibake
    (e.g. 'SaÃºl' -> 'Saúl', 'DuÅ¡an TadiÄ‡' -> 'Dušan Tadić').

    Tries cp1252 first (a superset of Latin-1 that also covers characters like the
    double-dagger '‡' produced when 0x87 is mis-decoded), then falls back to Latin-1.
    Safe to apply to already-clean strings: round-tripping plain ASCII is a no-op,
    and the except branches cover any string that isn't actually corrupted this way.
    """
    if not isinstance(s, str):
        return s
    for encoding in ("cp1252", "latin1"):
        try:
            return s.encode(encoding).decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return s


def normalize_name(s) -> str | None:
    """Mojibake-fix, strip accents (NFKD), lowercase, strip punctuation, collapse whitespace."""
    if pd.isna(s):
        return None
    s = fix_mojibake(str(s))
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s
