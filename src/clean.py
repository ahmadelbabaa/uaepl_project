"""Cleaning for Players.xlsx: dedup by (normalized name, team), not by player_id alone.

See plan.md Section 2 for the full reasoning. Two distinct duplicate patterns exist
in the raw file:
  1. Same player_id, different normalized name -> a genuine id collision (two real
     players sharing an id). These must NOT be merged.
  2. Same normalized name, same team, repeated/near-duplicate rows (possibly under
     different player_ids) -> the same registration captured more than once. These
     collapse to a single row per (name, team).
Weight/height are intentionally not carried forward here -- they were investigated
(a column-swap bug, zero-as-missing placeholders) but dropped from the final model
(see plan.md Section 3), since they add no value to the category-performance analysis.
"""
import pandas as pd

from .text_utils import normalize_name

# Players.xlsx team_id (a team name string) -> stats.xlsx team_name
TEAM_CROSSWALK = {
    "Ajman": "Ajman Club",
    "Al Ain": "Al Ain FC",
    "Al Dhafra": "Al Dhafra FC",
    "Al Jazira": "Al Jazira Club",
    "Al Nasr": "Al Nasr SC",
    "Al Wahda": "Al Wahda FC",
    "Al Wasl": "Al Wasl Club",
    "Al-Bataeh": "Al Bataeh Club",
    "Baniyas": "Bani Yas Club",
    "Dibba": "Dibba FC",
    "Kalba": "Al Ittihad Kalba",
    "KhorFakkan": "Khorfakkan Club",
    "Shabab Al Ahli Dubai": "Shabab Al Ahli Club",
    "Sharjah": "Sharjah FC",
    "AL Sadd - Qatar": None,
}

# Players.xlsx position vocabulary -> stats.xlsx position vocabulary
POSITION_CROSSWALK = {
    "Defense": "Defender",
    "Middle": "Midfielder",
    "Attack": "Attacker",
    "Goal Keeper": "Goalkeeper",
}


def clean_players(raw_players: pd.DataFrame) -> pd.DataFrame:
    """Dedup Players.xlsx to one row per (normalized name, team).

    Returns columns: player_id, player, name_key, player_category, position,
    team_id, team_name_mapped, season.
    """
    df = raw_players.copy()
    df["name_key"] = df["player"].map(normalize_name)
    df["team_name_mapped"] = df["team_id"].map(TEAM_CROSSWALK)
    df["player_category"] = df["player_category"].str.strip()
    df["position"] = df["position"].str.strip()

    deduped = (
        df.sort_values(["name_key", "team_id"])
        .groupby(["name_key", "team_id"], as_index=False)
        .agg(
            {
                "player_id": "first",
                "player": "first",
                "player_category": "first",
                "position": "first",
                "team_name_mapped": "first",
                "season": "first",
            }
        )
    )
    return deduped
