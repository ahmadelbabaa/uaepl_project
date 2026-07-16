"""Single entry point: ingest -> clean/match -> build database.

Run with: python run_pipeline.py
Reproduces data/uaepl.db and data/qa/player_match_report.csv from the two raw
source files in data/Players.xlsx and data/stats.xlsx, with no manual steps.
"""
from src.build_db import DB_PATH, QA_REPORT_PATH, build_database
from src.ingest import load_players_raw, load_stats_raw


def main() -> None:
    print("Ingesting raw source files...")
    raw_players = load_players_raw()
    raw_stats = load_stats_raw()
    print(f"  Players.xlsx: {len(raw_players)} rows")
    print(f"  stats.xlsx:   {len(raw_stats)} rows")

    print("Cleaning, matching, and building the database...")
    build_database(raw_players, raw_stats)

    print(f"Done. Database written to: {DB_PATH}")
    print(f"Match QA report written to: {QA_REPORT_PATH}")


if __name__ == "__main__":
    main()
