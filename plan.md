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
- Apply plausibility bounds first: weight outside 45–120kg or height outside 150–210cm
  → set to NULL (found a clear typo: id 373923 weight 59 vs 159 within the same
  player+team group). Document the bounds and count nulled as a QA metric.
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
- **Team crosswalk**: a small hardcoded mapping (14 rows) between Players.xlsx's
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
- **Final measured result: 449/456 (98.5%) confirmed**, 4 ambiguous (0.9%), 3 unmatched
  (0.7%) — 7 genuinely uncertain cases total (450/3/3 after the Rodrigo manual override
  below, i.e. 6 uncertain, 1.3%). These are real, irreducible limitations of the source
  data, not a matching-logic gap — e.g. two actually-different Brazilian players both
  simply named `"Rodrigo"` on Al Wasl Club (stats.xlsx never records a surname for
  him). No amount of string-matching sophistication resolves "the source only gave a
  first name," so these are correctly left unmatched/logged rather than guessed.
- **Manual override mechanism**: for a small number of specific ambiguous cases, the
  user manually verified the correct player via external research (Transfermarkt) —
  e.g. confirmed only one Brazilian defender named Rodrigo plays for Al Wasl in
  2025/26, resolving that case to `"Rodrigo Oliveira D'Almeida"`. Rather than treating
  this as ad hoc, it's implemented as a small, explicit override table (e.g.
  `data/manual_overrides.csv`: stats `player_id` → confirmed players.xlsx identity,
  with a comment citing the source and date) applied in code after the automated
  matching stage — reproducible and auditable, not a hand-edit of the source files.
  This is reserved for specific verified cases, not a general strategy; remaining
  ambiguous/unmatched cases without a manual override stay logged as a documented
  limitation given the assessment's time constraints.
- Confirmed, ambiguous, and unmatched outcomes are all written to a small QA log
  (`data/qa/player_match_report.csv`) rather than silently guessing — this is the
  explicit "matching logic" documentation the assessment asks for.
- Unmatched/ambiguous stats players are **not dropped** from the model — they still get
  their own `player_key` (derived from the stats file) so their match stats remain
  analyzable; they simply lack Players.xlsx enrichment (category/position, and no
  club-registration row), documented as a known limitation (~2% unenriched/ambiguous).

## Section 3 — Data Model (decided)

Six tables — no `competitions` table, since neither source has any competition-level
data (single league, no signal to build one from); manufacturing an empty/placeholder
table would be structure the data doesn't support.

### Dimensions
- **`teams`** — grain: one row per team (~14-15, from the Section 2 crosswalk).
  `team_key` (surrogate PK), `canonical_name`, `players_source_name` (e.g. "Sharjah"),
  `stats_source_name` (e.g. "Sharjah FC") — both source spellings kept for traceability.
- **`players`** — grain: one row per unique player identity (the stable entity from
  Section 2: name, category, position — confirmed invariant across all duplicate/stint
  groups). `player_key` (surrogate PK), `canonical_name`, `player_category`
  (Local/Resident/Foreign), `position` (standardized to stats.xlsx's vocabulary —
  Goalkeeper/Defender/Midfielder/Attacker — via the Section 2 crosswalk, since that's
  the more conventional English terminology), `source_player_id_players`,
  `source_player_id_stats` (both nullable — an ambiguous/unmatched stats player has no
  roster id; a roster player with no recorded stats has no stats id), and
  **`match_status`** (`confirmed` / `manual_override` / `ambiguous` / `unmatched`) so
  the ~1.3% of imperfectly-linked players are visibly flagged in the model itself, not
  a fact only living in a README.
- **`matches`** — grain: one row per match (121 distinct `match_id`s). `match_key`,
  `match_date`. No richer metadata exists (confirmed in Section 2).

### Bridges
- **`player_club_registrations`** — grain: one row per (player, team, season).
  `player_key` FK, `team_key` FK, `season`, `weight`, `height` (as recorded at that
  specific club — preserves transfer history per the earlier decision not to collapse
  multi-team rows).
- **`match_teams`** — grain: one row per (match, team), normally 2 rows per match.
  `match_key` FK, `team_key` FK. No home/away distinction, since that data isn't
  available.

### Fact
- **`player_match_stats`** — grain: one row per (player, match, stat_type) — mirrors
  stats.xlsx's natural long format exactly, avoiding hardcoding the 36 stat types into
  the schema. `player_key` FK, `match_key` FK, **`team_key` FK stored directly on this
  table** (not inferred via `player_club_registrations`), since a player's match-day
  team can differ from their season-level registration if they transferred mid-season.
  `stat_type`, `stat_value`.

## Section 4 — Database Creation & Loading (not yet planned)

## Section 5 — Analytical Output (not yet planned)
