"""Ingestion: load the two raw source files as-is, no cleaning/transformation."""
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PLAYERS_XLSX = DATA_DIR / "Players.xlsx"
STATS_XLSX = DATA_DIR / "stats.xlsx"


def load_players_raw() -> pd.DataFrame:
    """Load data/Players.xlsx exactly as provided, no cleaning."""
    return pd.read_excel(PLAYERS_XLSX, sheet_name=0)


def load_stats_raw() -> pd.DataFrame:
    """Load data/stats.xlsx exactly as provided, no cleaning."""
    return pd.read_excel(STATS_XLSX, sheet_name=0)
