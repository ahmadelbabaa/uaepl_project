# UAEPL Technical Assessment — Football Data Pipeline Plan

## Context

This is a technical assessment (deadline: Friday 17 CET 12:00) requiring an end-to-end
data pipeline: ingest two football datasets, clean/transform/integrate them, build a
structured data model, load into a database, and produce an analytical output about
player categories. The working repo (`uaepl_project`) is currently empty (just a
placeholder README) — a private GitHub repo, so committing source data is acceptable.

The two datasets were received as `.xlsx` attachments via email (not a URL/API), already
placed locally at `data/Players.xlsx` and `data/stats.xlsx` (untracked, not yet committed).
Actual inspected structure:
- **`data/Players.xlsx`** (1,011 rows, 1 sheet): `player_id, player, player_category
  (Local/Resident/Foreign), weight, height, position (Defense/Middle/Attack/Goal Keeper),
  team_id (actually a team NAME string, e.g. "Sharjah"), season (constant "2025/2026")`.
- **`data/stats.xlsx`** (42,073 rows, sheet `query-results-1784109735434`): Opta-style long
  format, grain = one row per (match_id, player_id, stat_type): `match_id, team_id (opaque
  hash), player_id (opaque hash, NOT the same id-space as Players.xlsx), stat_type (36
  distinct values: goals, totalPass, minsPlayed, yellowCard, etc.), stat_value, nationality,
  team_name (e.g. "Sharjah FC"), player_name, player_name_short, date_of_birth, position
  (Midfielder/Defender/Attacker/Goalkeeper), match_date`. 456 distinct players, 121 distinct
  matches, dates span 2025-12-28 to 2026-05-16.
- Confirmed: `player_id` spaces are **completely disjoint** between the two files (int-like
  vs opaque hash) — integration must happen via name matching, not id join.

The brief's "do not manually download/edit/prepare" instruction is interpreted as: no
manual cleaning or editing of the source files in Excel, and no manual retyping/copy-paste
of data — all retrieval and transformation must happen in code. Since there is no
external URL/API for these specific files (they were emailed as attachments), the
literal "retrieve programmatically from the provided sources" is satisfied by treating
the committed raw files as the fixed source-of-truth input and having a script load them
via `pandas`/`openpyxl` — not a network fetch. This assumption will be documented
explicitly in the README.

## Section 1 — Data Ingestion (decided)

- Raw files committed unmodified at their current path:
  - `data/Players.xlsx`
  - `data/stats.xlsx`
  (repo is private, so committing source data was confirmed acceptable by the user)
- Ingestion module `src/ingest.py` with one function per source:
  - `load_players_raw() -> pd.DataFrame` — `pandas.read_excel(path, sheet_name=0)`
  - `load_stats_raw() -> pd.DataFrame` — same pattern
  - Each function only loads — no cleaning/renaming/filtering at this stage, to keep a
    clean separation between "what we received" and "what we transformed."
- Both raw DataFrames are also persisted into "landing" tables in the database
  (`raw_players`, `raw_player_match_stats`) untouched, so the whole pipeline from raw
  file → raw table → clean model can be re-run end to end and inspected at each stage.
- Single entry-point script (`run_ingest.py` or a `make ingest` / CLI target) that a
  fresh clone can run immediately after `pip install -r requirements.txt` — no manual
  steps, satisfying "repeatable" and "people I share with have access."
- `requirements.txt` will pin `pandas`, `openpyxl` (xlsx engine), and later the DB
  driver (`sqlite3` is stdlib; add `duckdb` if that's the chosen engine).
- README will document: the assumption above about "programmatic retrieval," exact
  file provenance (received via email, Opta-sourced stats), and file placement.

## Section 2 — Clean & Transform (decided)

### Players.xlsx cleaning
- **Weight/height column swap (found via user inspection)**: 9 rows have `weight` and
  `height` literally swapped (e.g. `weight=185, height=74` — physically backwards for
  an adult, since weight in kg is never greater than height in cm; `weight=74,
  height=185` is a normal footballer). Detected via `weight > height` (true for exactly
  9 rows across the whole file) and **swapped back rather than nulled** — this
  recovers 9 good data points instead of discarding them, which a naive bounds-only
  check would have done.
- **Then** apply plausibility bounds: weight outside 45–120kg or height outside
  150–210cm → set to NULL. After the swap fix, this catches: (a) 25 rows where both
  `weight==0` and `height==0` together — confirmed as this source's missing-data
  placeholder (not a swap candidate, since both fields are zero together), correctly
  nulled; (b) one genuine typo (id 373923, weight 159 vs a paired row's 59 for the same
  player+team — an extra leading digit, not a swap since 159 > 160 height is false, i.e.
  159 < 160, so it doesn't trigger the swap rule but does trigger the bounds check).
  Document counts of swapped vs. nulled as a QA metric — important to distinguish
  "recovered" from "discarded" in the README.
- Handle repeated `player_id` values (19 rows across 9 ids found), which are of two
  genuinely different kinds — must be distinguished by normalized name, not id alone:
  1. **Same id, different normalized name** (ids 450720, 669420 — a genuine source data
     bug: two different real players collide on the same id) → treated as **two distinct
     players**, never merged. This is why the model mints its own surrogate `player_key`
     from (player_id, normalized name), not raw `player_id`, as identity.
  2. **Same id, same normalized name, different team** (ids 86907, 200072, 257742,
     552979, 755001) → confirmed same physical player, category/position identical
     across the rows, only team (and weight/height) differ → a genuine mid-season
     transfer. **Decision: do not collapse these — keep one row per (player, team)** so
     transfer/season history stays analyzable, rather than picking a single "current"
     team and discarding the rest.
  3. **Same id, same name, same team, repeated row** (id 841086 "Oumar Keita": one exact
     duplicate + one row differing only by a noisy weight value) → this is not a stint,
     just repeated/noisy capture of the same registration → collapse to one row per
     (player, team), preferring the plausibility-checked value when they conflict.
  4. **Separately discovered**: duplicate (name, team) registrations under **different**
     `player_id`s — 20 (name, team) pairs affected, 18 involving genuinely different
     ids (e.g. "Adama Alain Diallo" registered twice for Al Wasl under two different
     ids). The dedup key must therefore be **(normalized name, team)**, not `player_id`
     alone — deduping only by id (as first attempted) leaves these duplicates in place
     and they were the single biggest cause of false ambiguity in the stats-matching
     step below (candidates that looked like "2 different people" were actually the
     same person listed twice).
- Standardize `player_category` (Local/Resident/Foreign) and `position` values: trim
  whitespace, fix casing, keep as a small fixed category set (no free text expected).
- **Grain change**: rather than force "Players.xlsx" into a single players table, split
  it into `players` (stable identity: player_key, name, category, position — confirmed
  invariant across all duplicate groups) and `player_club_registrations` (player_key,
  team, weight, height, season — grain = one row per player per team per season). This
  is what naturally supports "did this player play for multiple clubs" without
  fabricating a canonical weight/height when the source itself disagrees across teams.

### stats.xlsx cleaning
- No duplicate (match_id, player_id, stat_type) rows found — grain is already clean.
- Stats are simple counting stats (goals, passes, cards, etc.); if duplicates appear
  after joins/reloads, the rule is sum `stat_value` per (match_id, player_id, stat_type)
  rather than drop, since repeated events (e.g. two goals) are legitimate.
- Keep grain **long/normalized** (one row per player-match-stat_type) rather than pivoting
  to wide columns in the storage table — avoids hardcoding the 36 stat types into the
  schema; a wide pivot is built as a view/query for analysis instead (see Section 3).

### Cross-source integration (players ↔ stats)
- The two files' `player_id` values are in **completely disjoint id spaces** (confirmed:
  0 overlap) — integration is only possible via name + team matching.
- **Team name dictionary**: a small hardcoded mapping (14 rows) between Players.xlsx's
  `team_id` (a team *name* string, e.g. "Sharjah") and stats.xlsx's `team_name`
  (e.g. "Sharjah FC"), since naming conventions differ per file but both lists are
  small and stable. One Players.xlsx team ("AL Sadd - Qatar") has no counterpart in
  stats.xlsx and maps to null (likely a pre-season friendly/loan entry never reflected
  in league match stats).
- **Mojibake fix (important discovered issue)**: stats.xlsx name fields contain real
  encoding corruption, not just accents — e.g. `"Saúl"` is literally stored as
  `"SaÃºl"`, `"Gerónimo"` as `"GerÃ³nimo"` (classic UTF-8-bytes-decoded-as-Latin-1
  mojibake, likely an artifact of the Opta export pipeline). Before normalization,
  attempt `s.encode('latin1').decode('utf-8')` and use the repaired string if it
  succeeds — this recovers the real accented name so accent-stripping normalization
  then matches correctly. This specifically fixes matching for Spanish/Portuguese/
  other Latin-script foreign players, which is exactly the pattern you flagged.
- **Name matching algorithm**: normalize names in both files (mojibake-fix, then
  Unicode NFKD strip accents, lowercase, strip punctuation, collapse whitespace) into
  token sets. Stats.xlsx has two name fields (`player_name`, `player_name_short`) whose
  "short/full" meaning is inconsistent across nationalities (for UAE nationals
  `player_name_short` is often the *fuller* legal name; for foreign players it's often
  a nickname/mononym). **Important fix**: matching must *prefer whichever field has
  more tokens (the fuller name) and only fall back to the shorter field if the full
  name finds nothing** — taking the union of matches from both fields (trying both
  independently and merging results) was tested and rejected, because a generic
  short field like "Ahmed" or "Rodrigo" can trivially subset-match several *other*
  unrelated candidates on the same team, injecting false ambiguity even when the fuller
  name field alone would have matched uniquely. For each distinct stats-file player,
  candidates are restricted to the same (crosswalked) team, then matched via the
  preferred (fuller) field first.
- **Stage ordering bug (important, found late)**: the matching stages must be tried in
  this exact order — **exact-on-full, then fuzzy-on-full, then exact-on-fallback, then
  fuzzy-on-fallback** — fully exhausting the longer/fuller name field (both exact and
  fuzzy) before ever trying the shorter fallback field. Originally the fuzzy fallback
  stages were both ordered after both exact stages, which caused a real bug: stats
  entry `"Ahmed Abdulla Mohamad Jashak"` (short: `"Ahmed"`) has a genuine roster match
  `"Ahmad Abdallah Mohamed Jashak"` (86% fuzzy-similar on the full name — every word is
  a transliteration variant: Ahmed/Ahmad, Abdulla/Abdallah, Mohamad/Mohamed), but the
  generic single-word fallback `"Ahmed"` **exactly** matched two *other*, wrong
  Khorfakkan players first (a goalkeeper and midfielder genuinely spelled "Ahmed"),
  and since exact-on-fallback ran before fuzzy-on-full, the function returned those 2
  wrong candidates and never got to try the correct fuzzy match at all. A weak exact
  match on a generic field must not be allowed to preempt a strong fuzzy match on a
  specific field — fixing the stage order resolved this cleanly to the single correct
  candidate.
- **Four-stage matching** (corrected order):
  1. *Exact-on-full*: token-subset on the preferred/fuller field.
  2. *Fuzzy-on-full*: per-token similarity (stdlib `difflib.SequenceMatcher` ratio ≥
     0.85) on the same fuller field — catches transliteration variants like
     `"Yaslam"` vs `"Yaslem"`, `"Al Naqbi"` vs `"Alnaqbi"` word-joining, `"Bu Sanda"`
     vs `"Busanda"`, or `"Ahmed Abdulla Mohamad Jashak"` vs `"Ahmad Abdallah Mohamed
     Jashak"`.
  3. *Exact-on-fallback*: only tried if the full field found nothing at all.
  4. *Fuzzy-on-fallback*: last resort.
  Stages 1–2 alone give 418/456 (92%) confirmed before any tie-break, 3 unmatched, 35
  ambiguous.
  3. *Tie-break stage*, applied only when name matching still returns >1 candidate
     (never applied to, or allowed to override, an already-unique name match):
     a. **Position tie-break**: both files record position under different label sets
        (`Defender/Midfielder/Attacker/Goalkeeper` in stats.xlsx vs
        `Defense/Middle/Attack/Goal Keeper` in Players.xlsx — mapped 1:1). Among tied
        name-candidates, keep only those whose position agrees; accept only if this
        narrows to exactly one. (Tested using position as a hard filter *before* name
        matching too — that backfired, since roster-registered position and actual
        match position don't always agree. So it's a tie-breaker only, not a
        precondition.)
     b. **Nationality/category tie-break**: stats.xlsx's `nationality` vs Players.xlsx's
        `player_category` (Local/Resident/Foreign) — UAE nationality should mean
        "Local", non-UAE should mean "Resident"/"Foreign". Applied as a last resort on
        whatever's still tied after the position tie-break.
     c. **Positional first-token tie-break**: when the stats file's short name is a
        single word (e.g. "Ahmed"), only among *already-tied* candidates, prefer those
        whose name's first token equals that word (i.e. treat it as their actual given
        name) over candidates where it only appears as a middle name. Narrowed the
        Khorfakkan "Ahmed" case from 8 spurious candidates (anyone with "ahmed"
        anywhere in their name) down to the 2 players genuinely named Ahmed on that
        squad. **Important**: this must be a tie-break among candidates that already
        passed name matching, never a precondition — applying it as a blanket
        first-name-only rule was tested and rejected, since some short names are
        derived from a middle name/surname instead (e.g. "Rangel" as a nickname for
        "Vinicius Rangel Da Silva" — requiring first-token match there would have
        wrongly excluded the correct candidate).
     d. **Global uniqueness / claim-exclusion**: once a Players.xlsx row is uniquely
        matched to one stats player, it's removed from the candidate pool for every
        other stats player, then remaining ambiguous cases are re-evaluated. This
        fixed a real gap: two of the Khorfakkan "Ahmed" candidates for the attacker
        `"Ahmed Abdulla Mohamad Jashak"` (a goalkeeper and a midfielder) turned out to
        already be confidently matched to *other* stats players ("Ahmed Al Hosani" and
        "Ahmed Barman" respectively) — they were never real candidates for the
        attacker, just unclaimed noise. After exclusion, the attacker resolves to
        **zero** candidates, confirming he's genuinely absent from the roster file
        rather than "ambiguous among 2 people." This also cleanly resolved
        `"Mohammed Khalaf"` to a single confirmed match.

### ⚠️ Important documentation note: player counts genuinely differ between the two sources
This must be called out explicitly in the README — it is not a data-quality bug, it
reflects what each source actually represents:
- Players.xlsx has **989 player-team registrations** (956 unique physical players,
  since some players have 2 team registrations — see the dedup logic above) — this is
  the **full registered squad list** for each club.
- stats.xlsx has **456 unique players**. Summed per-team, this rises to **474**,
  because **18 of those 456 players have recorded appearances for 2 different teams**
  within the season (transfers) — i.e. genuinely counted once per team they played for,
  not a duplication bug. (438 players appear for exactly 1 team, 18 for exactly 2, none
  for more than 2.)
- Per-team, the roster (Players.xlsx) numbers run much higher (52–95 per club) than the
  stats.xlsx numbers (28–42 per club, or 30–42 before deduping the 18 transfer cases).
  **Interpretation**: Players.xlsx represents each club's full registered squad
  (including reserve/B-team players who never make a first-team matchday squad. Checking the Pro League website, this seems to be the "Professional"  and "Amateur" teams.), while
  stats.xlsx only contains players with **at least one recorded appearance in the Pro
  League** itself. This is why **510 of the 956 unique roster players (~53%) have zero
  recorded matches** — 25 of those are on "AL Sadd - Qatar" (a team with no presence in
  stats.xlsx at all), and the other 485 are real UAEPL-club players who are registered
  but simply never featured in a recorded league match this season.
- This should be stated plainly in the README's "known limitations" / "key assumptions"
  section so a reader doesn't mistake the ~53% non-appearance rate for a matching
  failure — it's the expected shape of the data (full squad list vs. actual match
  participants).

## Section 3 — Data Model (decided)

### Core analytical enrichment: merging `player_category` onto stats.xlsx
The brief specifically emphasizes analyzing the impact/performance of Local vs.
Resident vs. Foreign players — `player_category` is the central analytical axis for
Section 5, and it's worth being explicit about where it comes from and why:
- `name`, `position`, and `team` exist **independently in both files** — the entire
  point of the Section 2 name/team/position matching pipeline was to answer "which
  stats.xlsx `player_id` corresponds to which Players.xlsx identity?" Once that link
  exists, those overlapping fields don't need to be carried over from Players.xlsx as
  separate values — stats.xlsx's own versions are already complete for all 456 players
  (Players.xlsx's versions only exist for whoever we successfully matched), so they
  remain authoritative for anyone with stats.
- `player_category` (Local/Resident/Foreign), and weight/height, are the **only
  genuinely new information** Players.xlsx contributes once matching is done — this is
  the actual value of the whole matching exercise, not a side effect of it.
- **Practical sourcing rule for the `players` table**: `name`/`position` are sourced
  from stats.xlsx, and `player_category` is merged in from the matched Players.xlsx row
  via the Section 2 pipeline. Ambiguous/unmatched stats players (the ~6-10 documented
  edge cases) get `player_category = NULL`, consistent with the existing "not dropped,
  just unenriched" decision.
- **Scoping decision (important)**: the ~510 roster-only players with **zero** recorded
  stats (documented in the note above) are **excluded from the `players` table
  entirely** — they don't get merged in. They have no match participation to analyze,
  so including them would only add empty/null rows with no analytical value for the
  category-performance comparison the brief asks for. `players` grain is therefore
  "one row per unique player who appears in stats.xlsx" (456, of which ~446-450 get a
  `player_category` and ~6-10 stay NULL), not "every registered player league-wide."
  The 510-player finding stays fully documented as a data-characteristic note (README),
  it's just not modeled as rows in this table.

**Four tables** (simplified down from an initial six — `matches` and `match_teams`
were dropped as too thin to justify their own table, and `player_club_registrations`
was narrowed to a small side table only for the minority of players with 2 clubs).
No `competitions` table either, since neither source has any competition-level data.

### `teams`
Purpose: canonical reference reconciling the two different team-naming conventions
used across the sources. Grain: one row per team (~14-15, from the Section 2
crosswalk). `team_key` (surrogate PK), `canonical_name`, `players_source_name` (e.g.
"Sharjah"), `stats_source_name` (e.g. "Sharjah FC").

### `players`
Purpose: the core player dimension — identity, position, and (critically)
`player_category`, the central axis for the category-performance analysis the brief
asks for — plus each player's primary club, folded in directly rather than requiring
a join for the common case. Grain: one row per unique player **who appears in
stats.xlsx** (456 total) — roster-only players with zero recorded stats are excluded
(see scoping decision above; they don't get merged in since they have no match
participation to analyze).
- `player_key` (surrogate PK)
- `canonical_name`, `position` — sourced from stats.xlsx (standardized to its
  vocabulary — Goalkeeper/Defender/Midfielder/Attacker — since it's already complete
  and authoritative for every row at this grain)
- `player_category` (Local/Resident/Foreign) — merged in from the matched Players.xlsx
  row via the Section 2 pipeline; NULL for the ~6-10 ambiguous/unmatched edge cases
- `team_key` (FK) — the player's **primary club**. For the ~416 single-club players
  this is simply their one team. For the ~18-30 players who played for 2 different
  clubs, primary = **most recent club by last match_date** (same rule used for the
  earlier per-team headcount table, for consistency); their earlier club
  goes into `player_transfers` below instead of being dropped
- `source_player_id_players`, `source_player_id_stats` (nullable — unmatched/ambiguous
  players have no roster id)
- `match_status` (`confirmed` / `manual_override` / `ambiguous` / `unmatched`) — so the
  small fraction of imperfectly-linked players are visibly flagged in the model itself

**Dropped: `weight`/`height`**. These were investigated thoroughly in Section 2 (the
column-swap bug affecting 9 rows, the 25 zero-as-missing rows, the one-off typo) and
that finding stays fully documented in the README as a real data-quality catch — but
the fields themselves are excluded from the final model: they don't serve the
Local/Resident/Foreign performance analysis the brief centers on, and between the
swap-bug rows, the zero-placeholder rows, and the ~6-10 unmatched/ambiguous players
with no roster link at all, a meaningful share would be NULL regardless. Simpler to
document the finding than to carry partially-unreliable columns into the schema for no
analytical benefit.

### `player_transfers`
Purpose: a deliberately small side table that exists **only** to avoid silently
dropping a player's earlier club when they transferred mid-season — everyone with a
single club simply has no row here at all. This was chosen over collapsing multi-club
players into one row (data loss) and over a full `player_club_registrations` bridge
for every player (unnecessary for the ~93% with just one club). Grain: one row per
(player, additional prior team) — expected ~18-30 rows total. `player_key` (FK),
`team_key` (FK, the non-primary/earlier club). No weight/height here, since the
interesting fact is simply "this player also played for this team," not a second
biometric snapshot.

### `player_match_stats`
Purpose: the core fact table — every recorded performance metric, kept in its natural
long format so no stat type is hardcoded into the schema; this is what
Section 5's category-based analysis (Local vs. Resident vs. Foreign performance)
will aggregate over. Grain: one row per (player, match, stat_type) — mirrors
stats.xlsx's own grain exactly. `player_key` (FK), `match_id`, `match_date` (both
inlined directly here rather than via a separate `matches` table, since that
dimension held nothing beyond these two fields), `team_key` (FK, stored directly —
**not** inferred from `players.team_key` — so a player's match-day team is always
correct even when it differs from their current primary club), `stat_type`,
`stat_value`. Which two teams contested a given match (previously `match_teams`) is
fully derivable via `SELECT DISTINCT team_key WHERE match_id = X` on this table, so it
isn't materialized separately.

## Section 4 — Database Creation & Loading (decided)

**Engine: SQLite** (stdlib `sqlite3` — no extra dependency; single portable `.db` file
anyone can open with any SQLite browser; satisfies "include the completed database
file" as a literal deliverable). All three allowed engines were equally valid; SQLite
is the simplest given the assessment's emphasis on workflow over performance/scale.

**Schema** — 6 physical tables: the 2 raw landing tables from Section 1
(`raw_players`, `raw_player_match_stats` — unmodified copies, for lineage/auditability
of the raw→clean pipeline) plus the 4 clean model tables from Section 3.

```sql
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
```

**Loading & repeatability**: `src/build_db.py` deletes any existing `data/uaepl.db`
and rebuilds fresh every run (no `DROP TABLE IF EXISTS` accumulation risk, no
duplicate rows on re-run — critical for the "repeatable" requirement), then loads
each table in dependency order: `teams` → `players` → `player_transfers` →
`player_match_stats`, sourced from the already-validated Section 2 cleaning/matching
logic (which gets consolidated from scratch-tested snippets into real modules here).

**File layout**:
```
src/ingest.py     — load_players_raw(), load_stats_raw()
src/clean.py      — name+team dedup (weight/height swap-fix investigated but not
                    implemented — those columns were dropped from the final model,
                    see Section 3; the finding stays documented in the README)
src/match.py      — mojibake fix, normalize, 4-stage matching, tie-breaks,
                    claim-exclusion, manual overrides
src/build_db.py   — schema DDL + load logic
run_pipeline.py   — top-level: ingest → clean → match → build_db, one command
requirements.txt  — pandas, openpyxl
data/manual_overrides.csv       — the Rodrigo override (stats player_id -> roster identity)
data/qa/player_match_report.csv — confirmed/ambiguous/unmatched log (generated)
data/uaepl.db                   — the deliverable database (generated, committed)
```

A fresh clone + `pip install -r requirements.txt` + `python run_pipeline.py`
reproduces the entire database from the two raw source files with no manual steps.

## Section 5 — Analytical Output (decided)

**Format**: a Streamlit app (`app.py`), reading directly from `data/uaepl.db` via
`pandas.read_sql`. Centered on the brief's explicit ask (analysis of player
categories) and the recruiters' specific emphasis on foreign-player impact.

**Global team filter**: a sidebar dropdown — every team plus an **"All Teams"**
option — scopes the *entire* dashboard, not just one chart. With "All Teams" selected,
every section below shows league-wide category comparisons; selecting a specific team
scopes every section down to that team's players only (e.g. "within Sharjah FC, how do
Local/Resident/Foreign players compare on goals-per-90"). This is what lets the
dashboard answer both "how does the league look overall" and "how does this
specifically play out within one squad."

**Sections** (as `st.tabs()`):
1. **Squad composition** — the core "impact" story. With "All Teams": a stacked bar
   chart of Local/Resident/Foreign composition per team (shows which clubs rely most
   on foreign talent) + league-wide KPI tiles. With a specific team selected: that
   team's own category breakdown (simple donut/bar) + that team's KPI tiles.
2. **Playing time by category** — bar chart of average minutes played per category,
   scoped by the team filter. Answers whether a category is actually getting on the
   pitch more/less, and serves as the per-90 normalization base for sections 3-4.
3. **Attacking output by category** — goals and assists **per-90-minutes** (not raw
   totals, since playing time differs by category) by category, faceted by position
   where relevant (this comparison mainly matters within Attackers/Midfielders).
4. **Passing by category** — pass accuracy % (`accuratePass / totalPass`) by category.
5. **Defensive performance by category** — **successful tackle %**
   (`wonTackle / totalTackle`) by category — a genuine defensive-skill signal,
   replacing the earlier "cards per 90" idea with something more analytically useful.
6. **Leaderboards** — top scorers/assists table, with category as a filterable
   column/badge, scoped by the team filter.

**Methodology note to state explicitly in the dashboard/README**: per-90 and
percentage-based metrics are used deliberately instead of raw counts, since comparing
raw totals across categories (or teams) would be misleading if one group tends to
play more minutes on average.
