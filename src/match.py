"""Cross-source matching: link each stats.xlsx player to a Players.xlsx roster identity.

Implements the full pipeline validated in plan.md Section 2:
  1. exact-on-full, 2. fuzzy-on-full, 3. exact-on-fallback, 4. fuzzy-on-fallback
     (in that exact order -- a weak exact match on a generic short name must never
     preempt a strong fuzzy match on the fuller name)
  then tie-breaks (position, nationality/category, positional-first-token) applied
  only among already-tied candidates, then a global claim-exclusion pass, then
  manual overrides for specific user-verified cases.
"""
import difflib
from pathlib import Path

import pandas as pd

from .clean import POSITION_CROSSWALK, clean_players
from .text_utils import normalize_name

FUZZY_THRESHOLD = 0.85
MANUAL_OVERRIDES_CSV = Path(__file__).resolve().parent.parent / "data" / "manual_overrides.csv"


def _tok_ratio(a_tokens: tuple, b_tokens: tuple) -> float:
    """Average best-match similarity of each token in the smaller set against the larger set."""
    small, large = (a_tokens, b_tokens) if len(a_tokens) <= len(b_tokens) else (b_tokens, a_tokens)
    if not small:
        return 0.0
    total = sum(
        max((difflib.SequenceMatcher(None, t, u).ratio() for u in large), default=0.0)
        for t in small
    )
    return total / len(small)


def prepare_stats_players(raw_stats: pd.DataFrame) -> pd.DataFrame:
    """One row per unique stats.xlsx player_id, with normalized name fields for matching."""
    df = raw_stats[
        ["player_id", "player_name", "player_name_short", "team_name", "position", "nationality"]
    ].drop_duplicates(subset=["player_id"]).copy()

    df["position_mapped"] = df["position"].map({v: k for k, v in POSITION_CROSSWALK.items()})
    df["is_uae"] = df["nationality"].eq("United Arab Emirates")
    df["name_key"] = df["player_name"].map(normalize_name)
    df["short_key"] = df["player_name_short"].map(normalize_name)
    df["tokens"] = df["name_key"].map(lambda s: tuple(s.split()) if s else tuple())
    df["short_tokens"] = df["short_key"].map(lambda s: tuple(s.split()) if s else tuple())

    def pick_full_short(row):
        a, b = row["tokens"], row["short_tokens"]
        return (a, b) if len(a) >= len(b) else (b, a)

    picked = df.apply(pick_full_short, axis=1)
    df["full_tokens"] = picked.map(lambda x: x[0])
    df["fallback_tokens"] = picked.map(lambda x: x[1])
    return df


def _exact_match(key_tokens: tuple, cands: pd.DataFrame) -> list:
    if not key_tokens:
        return []
    key_set = set(key_tokens)
    out = []
    for _, p in cands.iterrows():
        pt = set(p["tokens"])
        if pt and (key_set <= pt or pt <= key_set):
            out.append(p["row_id"])
    return list(set(out))


def _fuzzy_match(key_tokens: tuple, cands: pd.DataFrame) -> list:
    if not key_tokens:
        return []
    out = []
    for _, p in cands.iterrows():
        pt = p["tokens"]
        if pt and _tok_ratio(key_tokens, pt) >= FUZZY_THRESHOLD:
            out.append(p["row_id"])
    return list(set(out))


def find_candidates(row: pd.Series, players_dedup: pd.DataFrame) -> list:
    """Four-stage matching, restricted to the same (crosswalked) team."""
    cands = players_dedup[players_dedup["team_name_mapped"] == row["team_name"]]
    for fn in (
        lambda: _exact_match(row["full_tokens"], cands),
        lambda: _fuzzy_match(row["full_tokens"], cands),
        lambda: _exact_match(row["fallback_tokens"], cands),
        lambda: _fuzzy_match(row["fallback_tokens"], cands),
    ):
        m = fn()
        if m:
            return m
    return []


def tie_break(cands: list, row: pd.Series, players_dedup: pd.DataFrame) -> list:
    """Narrow an ambiguous candidate list; never applied to an already-unique match."""
    if len(cands) <= 1:
        return cands

    tokens_by_row = players_dedup.set_index("row_id")["tokens"]
    pos_by_row = players_dedup.set_index("row_id")["position"]
    cat_by_row = players_dedup.set_index("row_id")["player_category"]

    # (c) positional first-token tie-break, for single-word short names only
    for key in (row["full_tokens"], row["fallback_tokens"]):
        if len(key) == 1:
            first_tok_match = [c for c in cands if tokens_by_row.loc[c] and tokens_by_row.loc[c][0] == key[0]]
            if len(first_tok_match) == 1:
                return first_tok_match
            if first_tok_match:
                cands = first_tok_match

    # (a) position tie-break
    pos_filtered = [c for c in cands if pos_by_row.loc[c] == row["position_mapped"]]
    stage = pos_filtered if pos_filtered else cands
    if len(stage) <= 1:
        return stage

    # (b) nationality/category tie-break
    if row["is_uae"]:
        cat_filtered = [c for c in stage if cat_by_row.loc[c] == "Local"]
    else:
        cat_filtered = [c for c in stage if cat_by_row.loc[c] in ("Resident", "Foreign")]
    return cat_filtered if cat_filtered else stage


def match_all(raw_players: pd.DataFrame, raw_stats: pd.DataFrame) -> pd.DataFrame:
    """Run the full matching pipeline. Returns stats_players with a resolved `player_row_id`
    (nullable, references players_dedup.row_id) and `match_status` column.
    """
    players_dedup = clean_players(raw_players)
    players_dedup["row_id"] = players_dedup.index
    players_dedup["tokens"] = players_dedup["name_key"].map(lambda s: tuple(s.split()) if s else tuple())

    stats_players = prepare_stats_players(raw_stats)
    stats_players["cand_ids"] = stats_players.apply(lambda r: find_candidates(r, players_dedup), axis=1)
    stats_players["tb_cands"] = stats_players.apply(lambda r: tie_break(r["cand_ids"], r, players_dedup), axis=1)

    # (d) global uniqueness / claim-exclusion: a roster row already uniquely claimed
    # by one stats player is removed from every other candidate list, then remaining
    # ambiguous cases are re-evaluated once.
    claimed = set()
    for _, row in stats_players[stats_players["tb_cands"].map(len) == 1].iterrows():
        claimed.add(row["tb_cands"][0])

    def resolve(cands):
        if len(cands) <= 1:
            return cands
        remaining = [c for c in cands if c not in claimed]
        return remaining if remaining else cands

    stats_players["final_cands"] = stats_players["tb_cands"].map(resolve)

    def status_and_row(cands):
        if len(cands) == 1:
            return "confirmed", cands[0]
        if len(cands) == 0:
            return "unmatched", None
        return "ambiguous", None

    resolved = stats_players["final_cands"].map(status_and_row)
    stats_players["match_status"] = resolved.map(lambda x: x[0])
    stats_players["player_row_id"] = resolved.map(lambda x: x[1])

    stats_players = apply_manual_overrides(stats_players, players_dedup)
    return stats_players, players_dedup


def apply_manual_overrides(stats_players: pd.DataFrame, players_dedup: pd.DataFrame) -> pd.DataFrame:
    """Apply user-verified overrides (e.g. Rodrigo, confirmed via Transfermarkt) for
    specific cases the automated pipeline couldn't resolve on its own.
    """
    if not MANUAL_OVERRIDES_CSV.exists():
        return stats_players

    overrides = pd.read_csv(MANUAL_OVERRIDES_CSV)
    name_to_row = {
        (r["name_key"], r["team_id"]): r["row_id"] for _, r in players_dedup.iterrows()
    }

    stats_players = stats_players.set_index("player_id")
    for _, ov in overrides.iterrows():
        stats_player_id = ov["stats_player_id"]
        key = (ov["roster_name_key"], ov["roster_team_id"])
        row_id = name_to_row.get(key)
        if stats_player_id in stats_players.index and row_id is not None:
            stats_players.loc[stats_player_id, "player_row_id"] = row_id
            stats_players.loc[stats_player_id, "match_status"] = "manual_override"
    return stats_players.reset_index()
