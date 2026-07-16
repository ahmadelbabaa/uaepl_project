# UAEPL Player Category Analysis

An end-to-end data pipeline that ingests two UAE Pro League datasets (a player
roster file and a match-stats file), cleans and integrates them,
loads them into a SQLite database, and presents a Streamlit dashboard analyzing
player performance by category (Local / Resident / Foreign).

## Overall approach and architecture

```
data/Players.xlsx, data/stats.xlsx   (raw sources, committed unmodified)
        |
src/ingest.py        loads them, zero cleaning
        |
src/clean.py          dedupes Players.xlsx by (name, team)
src/match.py           links stats.xlsx players to Players.xlsx identities
src/text_utils.py      shared name-normalization / mojibake-fix helpers
        |
src/build_db.py        builds the schema and loads everything into data/uaepl.db
        |
run_pipeline.py         one command tying the whole thing together
        |
app.py                  Streamlit dashboard reading from data/uaepl.db
```

The two files share no common ID space — the whole
"integration" step is therefore a name + team matching problem, solved in
`src/match.py`. See **Key assumptions and data-quality decisions** below for
how that works and how well it performs.

The database and dashboard are scoped around **player category** as the
central analytical axis.

## How to set up and run the solution

```bash
pip install -r requirements.txt

# Rebuild the database from the two raw source files (idempotent — safe to re-run)
python run_pipeline.py

# Launch the dashboard
streamlit run app.py
```

`data/uaepl.db` is committed to the repo as the completed deliverable database,
but running `run_pipeline.py` always deletes and rebuilds it fresh, so the
repo never depends on a stale copy.

## Structure of the data model

Four tables, plus two raw "landing" tables that mirror the source files
unmodified (for lineage/auditability — `raw_players`, `raw_player_match_stats`).


- **`teams`** — one row per team (~14-15). Canonical reference reconciling the
  two different team-naming conventions used across the sources (e.g.
  Players.xlsx's `"Sharjah"` vs. stats.xlsx's `"Sharjah FC"`).
- **`players`** — one row per unique player **who appears in stats.xlsx**
  (456 total). Roster-only players with zero recorded stats are deliberately
  excluded — see limitations below. Holds identity (`canonical_name`,
  `position`, both sourced from stats.xlsx, which is complete for every row at
  this grain), `player_category` (Local/Resident/Foreign, merged in from the
  matched Players.xlsx roster row), each player's primary club (`team_key`),
  and `match_status` (`confirmed` / `manual_override` / `ambiguous` /
  `unmatched`) so imperfectly-linked players are visibly flagged rather than
  silently guessed.
- **`player_transfers`** — a deliberately small side table (18 rows) holding
  only the players who played for a *second* club during the season, so that
  fact isn't lost — everyone else with one club simply has no row here.
- **`player_match_stats`** — the fact table: one row per (player, match,
  stat_type), mirroring stats.xlsx's own long format exactly rather than
  hardcoding the 36 stat types into the schema. `team_key`, `match_id`, and
  `match_date` are stored directly on this table (not inferred from
  `players.team_key`), so a player's match-day team is always correct even
  when it differs from their current primary club.

No `competitions` table — neither source has any competition-level data to
populate one with.

## Key assumptions and data-quality decisions

**Players.xlsx deduplication.** The file has two distinct kinds of duplicate
rows, and they need opposite treatment:
- Same `player_id`, *different* name → a genuine source bug (two real players
  collide on one id, found twice). Treated as two distinct players — this is
  why the pipeline mints its own surrogate identity from (name, team) rather
  than trusting `player_id`.
- Same name, same team, repeated row (sometimes under a *different*
  `player_id`) → the same registration captured more than once. Collapsed to
  one row. Deduping only by `player_id` (the first approach tried) missed this
  second pattern entirely and was the single biggest cause of false ambiguity
  in matching.

**Weight/height were investigated, then dropped from the model.** Two real
issues were found in Players.xlsx: 9 rows had weight and height literally
swapped (`weight=185, height=74` is backwards — recoverable, not noise), and
25 rows used `0` as a missing-data placeholder for both fields. Rather than
building cleaning logic to fix and carry these fields through, they were
dropped from the final schema entirely: they don't serve the
Local/Resident/Foreign performance analysis the brief centers on, and a
meaningful share would be null regardless. The finding is kept here as a
documented data-quality catch rather than implemented cleanup.

**Cross-source matching (the core integration problem).** Since the two files
share no id space, every stats.xlsx player is matched to a Players.xlsx
identity by name, restricted to the same (crosswalked) team:
1. Stats.xlsx has two name fields whose "short/full" meaning is inconsistent
   by nationality — for UAE nationals the "short" field is often the *fuller*
   legal name; for foreign players it's often a nickname. Matching always
   tries the fuller (more tokens) field first, falling back to the shorter
   field only if the full name finds nothing.
2. Names are normalized (mojibake-fix, Unicode accent-stripping, lowercasing)
   before comparison. Stats.xlsx names contain real encoding corruption, not
   just accents — e.g. `"Dušan Tadić"` was literally stored as
   `"DuÅ¡an TadiÄ‡"` (UTF-8 bytes decoded as Windows-1252). Reversing this
   correctly fixed 57 foreign player names.
3. Matching runs exact-token-subset, then fuzzy (character-similarity)
   matching — in that order, and critically, both are exhausted on the fuller
   name *before* ever falling back to the shorter field. Getting this order
   wrong (trying the short field's exact match first) let a generic short name
   wrongly out-compete a correct fuzzy match on the full name in testing.
4. Remaining ties are broken by position, then nationality/category, then
   (only for single-word short names) treating the word as a first name
   rather than a middle name — each applied only among already-tied
   candidates, never as a precondition, since each was tested as a hard filter
   first and found to reject correct matches.
5. Once a roster row is uniquely claimed by one stats player, it's removed
   from every other candidate's pool, and remaining ties are re-evaluated —
   this catches cases where a wrong "candidate" was actually already the
   correct match for someone else.
6. One case (`"Rodrigo"` on Al Wasl Club, ambiguous between two real
   candidates by name alone) was resolved via a manual, user-verified override
   (checked against Transfermarkt) rather than guessed — implemented as a
   small override table (`data/manual_overrides.csv`) applied in code, not a
   hand-edit of the source data.

**Result: 449/456 (98.5%) confirmed automatically, plus 1 manual override —
450/456 (98.7%) — with only 3 ambiguous and 3 unmatched cases left**, all
logged transparently in `data/qa/player_match_report.csv` rather than forced.

**Player counts genuinely differ between the two sources — this is not a
bug.** Players.xlsx has 989 player-team registrations (956 unique players) —
the full registered squad per club, including reserve/unused players.
stats.xlsx has 456 unique players — only those with at least one recorded
league appearance. This is why **510 of the 956 roster players (~53%) have
zero recorded matches**: 25 are on `"AL Sadd - Qatar"`, a team with no
presence in stats.xlsx at all; the other 485 are real club players who simply
never featured in a recorded match this season. The `players` table is
scoped to the 456 stats-linked players only (see data model above) since
roster-only players have no performance to analyze.

## Known limitations and potential improvements

- **6 players (1.3%) remain unresolved** after every automated step: 3
  ambiguous (e.g. two different real players both simply named `"Rodrigo"` on
  the same team, with no surname recorded anywhere in stats.xlsx to
  disambiguate) and 3 unmatched (foreign players whose nickname diverges too
  far from their official registered name for automated matching). These are
  genuine limits of the source data, not a matching-logic gap — logged rather
  than guessed. A next step would be manually verifying a few more via
  Transfermarkt, as was done for the Rodrigo case.
- **Weight/height are not modeled**, by deliberate choice (see above) — a
  future iteration could reinstate them with the swap-fix applied, if a use
  case for them emerged.
- **No competition/venue/home-away data** exists in either source, so
  `matches` only ever had a date — this is why it was folded directly into
  the fact table rather than kept as its own dimension.
- **`player_transfers` is derived from stats.xlsx's own team field**, i.e. a
  player counts as multi-club only if they actually recorded stats for two
  different teams. A roster registration under a second club with zero
  recorded stats there (which does happen — a stale/unused registration) is
  not reflected as a transfer, since there's no match participation to back it.
- **Primary club for multi-club players** is chosen as "most recent club by
  last match_date." This is a reasonable, consistent rule, but an alternative
  (e.g. "club with the most matches played") would occasionally pick
  differently.
- **The team and position crosswalks are small hardcoded dictionaries**
  (14-15 teams, 4 positions). This is appropriate at this scale but wouldn't
  scale to a multi-season or multi-league dataset without a more general
  reference-table approach.
- **Fuzzy matching uses stdlib `difflib`** rather than a dedicated library
  (e.g. `rapidfuzz`) for zero added dependencies — sufficient at this dataset
  size, but a larger dataset would benefit from a faster, more configurable
  matcher.
- **No automated test suite.** Given the time constraints, correctness was
  validated by extensive manual inspection and cross-checking against the raw
  data (documented in `plan.md`) rather than unit tests. Adding tests around
  the matching pipeline (`src/match.py`) would be the highest-value next step
  for long-term maintainability.
