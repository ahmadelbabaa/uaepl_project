"""Schema creation and loading for the SQLite database (plan.md Section 4).

Deletes any existing data/uaepl.db and rebuilds fresh every run, so re-running the
pipeline never accumulates duplicate rows -- this is what makes it repeatable.
"""
import sqlite3
from pathlib import Path

import pandas as pd

from .clean import TEAM_CROSSWALK
from .match import match_all
from .text_utils import fix_mojibake

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "uaepl.db"
QA_REPORT_PATH = DATA_DIR / "qa" / "player_match_report.csv"

SCHEMA_SQL = """
CREATE TABLE raw_players (
    player_id INTEGER, player TEXT, player_category TEXT, weight REAL,
    height REAL, position TEXT, team_id TEXT, season TEXT
);

CREATE TABLE raw_player_match_stats (
    match_id TEXT, team_id TEXT, player_id TEXT, stat_type TEXT, stat_value INTEGER,
    nationality TEXT, team_name TEXT, player_name TEXT, player_name_short TEXT,
    date_of_birth TEXT, position TEXT, match_date TEXT
);

CREATE TABLE teams (
    team_key INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    players_source_name TEXT,
    stats_source_name TEXT
);

CREATE TABLE players (
    player_key INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    position TEXT,
    player_category TEXT,
    team_key INTEGER REFERENCES teams(team_key),
    source_player_id_players TEXT,
    source_player_id_stats TEXT,
    match_status TEXT CHECK (match_status IN ('confirmed','manual_override','ambiguous','unmatched'))
);

CREATE TABLE player_transfers (
    player_key INTEGER REFERENCES players(player_key),
    team_key INTEGER REFERENCES teams(team_key),
    PRIMARY KEY (player_key, team_key)
);

CREATE TABLE player_match_stats (
    player_key INTEGER REFERENCES players(player_key),
    match_id TEXT,
    match_date TEXT,
    team_key INTEGER REFERENCES teams(team_key),
    stat_type TEXT,
    stat_value INTEGER,
    PRIMARY KEY (player_key, match_id, stat_type)
);
"""


def build_teams_table() -> pd.DataFrame:
    """One row per team, from the Section 2 crosswalk."""
    rows = []
    for players_name, stats_name in TEAM_CROSSWALK.items():
        canonical = stats_name if stats_name else players_name
        rows.append(
            {
                "canonical_name": canonical,
                "players_source_name": players_name,
                "stats_source_name": stats_name,
            }
        )
    teams = pd.DataFrame(rows).reset_index(drop=True)
    teams["team_key"] = teams.index + 1
    return teams[["team_key", "canonical_name", "players_source_name", "stats_source_name"]]


def assign_primary_and_transfers(raw_stats: pd.DataFrame) -> tuple[dict, dict]:
    """For each stats player_id, determine their primary team (most recent match)
    and any secondary team(s) (in-season transfers reflected directly in match data).
    """
    per_player_team = (
        raw_stats.groupby(["player_id", "team_name"])["match_date"].max().reset_index()
    )
    primary = {}
    secondary = {}
    for player_id, grp in per_player_team.groupby("player_id"):
        grp_sorted = grp.sort_values("match_date", ascending=False)
        primary[player_id] = grp_sorted.iloc[0]["team_name"]
        secondary[player_id] = list(grp_sorted.iloc[1:]["team_name"])
    return primary, secondary


def build_players_and_transfers(
    raw_players: pd.DataFrame, raw_stats: pd.DataFrame, teams: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Returns (players_df, player_transfers_df, qa_report_df)."""
    stats_players, players_dedup = match_all(raw_players, raw_stats)
    primary_team, secondary_teams = assign_primary_and_transfers(raw_stats)

    stats_name_to_key = dict(zip(teams["stats_source_name"], teams["team_key"]))

    stats_players = stats_players.reset_index(drop=True)
    stats_players["player_key"] = stats_players.index + 1

    players_dedup_by_row = players_dedup.set_index("row_id")

    def resolve_field(player_row_id, field):
        if pd.isna(player_row_id):
            return None
        return players_dedup_by_row.loc[player_row_id, field]

    players_rows = []
    for _, row in stats_players.iterrows():
        player_row_id = row["player_row_id"]
        players_rows.append(
            {
                "player_key": row["player_key"],
                "canonical_name": fix_mojibake(row["player_name"]),
                "position": row["position"],
                "player_category": resolve_field(player_row_id, "player_category"),
                "team_key": stats_name_to_key.get(primary_team.get(row["player_id"])),
                "source_player_id_players": resolve_field(player_row_id, "player_id"),
                "source_player_id_stats": row["player_id"],
                "match_status": row["match_status"],
            }
        )
    players_df = pd.DataFrame(players_rows)

    transfer_rows = []
    for _, row in stats_players.iterrows():
        for team_name in secondary_teams.get(row["player_id"], []):
            team_key = stats_name_to_key.get(team_name)
            if team_key is not None:
                transfer_rows.append({"player_key": row["player_key"], "team_key": team_key})
    player_transfers_df = pd.DataFrame(transfer_rows, columns=["player_key", "team_key"])

    qa_report_df = stats_players[
        ["player_id", "player_name", "player_name_short", "team_name", "match_status"]
    ].rename(columns={"player_id": "stats_player_id"})

    return players_df, player_transfers_df, qa_report_df


def build_player_match_stats(raw_stats: pd.DataFrame, players_df: pd.DataFrame, teams: pd.DataFrame) -> pd.DataFrame:
    stats_id_to_key = dict(zip(players_df["source_player_id_stats"], players_df["player_key"]))
    stats_name_to_key = dict(zip(teams["stats_source_name"], teams["team_key"]))

    df = raw_stats[["player_id", "match_id", "match_date", "team_name", "stat_type", "stat_value"]].copy()
    df["player_key"] = df["player_id"].map(stats_id_to_key)
    df["team_key"] = df["team_name"].map(stats_name_to_key)
    df = df.drop(columns=["player_id", "team_name"])
    return df[["player_key", "match_id", "match_date", "team_key", "stat_type", "stat_value"]]


def build_database(raw_players: pd.DataFrame, raw_stats: pd.DataFrame) -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    QA_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    teams = build_teams_table()
    players_df, player_transfers_df, qa_report_df = build_players_and_transfers(raw_players, raw_stats, teams)
    player_match_stats_df = build_player_match_stats(raw_stats, players_df, teams)

    qa_report_df.to_csv(QA_REPORT_PATH, index=False)

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA_SQL)
        raw_players.to_sql("raw_players", conn, if_exists="append", index=False)
        raw_stats.to_sql("raw_player_match_stats", conn, if_exists="append", index=False)
        teams.to_sql("teams", conn, if_exists="append", index=False)
        players_df.to_sql("players", conn, if_exists="append", index=False)
        player_transfers_df.to_sql("player_transfers", conn, if_exists="append", index=False)
        player_match_stats_df.to_sql("player_match_stats", conn, if_exists="append", index=False)
        conn.commit()
    finally:
        conn.close()
