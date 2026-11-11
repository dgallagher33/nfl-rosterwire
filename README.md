# NFL RosterWire

Conservative NFL transaction proposals for SOFTDRINK NFL 2K28, the expanded ESPN NFL 2K5 mod.

RosterWire fetches live ESPN memberships for all 32 teams, joins nflverse player identities, matches existing game slots by unique normalized name and exact birthdate, and proposes team changes. It stages only explicitly approved changes to the `team` column.

**It does not generate game saves, create players, change ratings or depth charts, or prove Mod Studio import compatibility.** Missing players and uncertain releases remain review-only. No 2,317-player conversion is supported.

## Supported files and safety

The file workflow requires the known expanded Mod Studio CSV baseline: **4,096 primary records, 68 secondary records, 91 unique columns**, including `pool,index,team,first,last,position,birth_date`. Alternative exports are rejected until validated. All slot keys, row order, column order, secondary records and non-team cell values are preserved. CSV quoting/newlines may be normalized; preservation is at the cell level.

The original export is never overwritten. Staging requires the plan summary, an unchanged original SHA-256, unchanged proposal contents except `approval`, and an ESPN snapshot retrieved within 24 hours. A validated temporary CSV is published without overwriting an existing destination, alongside an exact change manifest.

## Windows setup

Python 3.10+ and Git are required. In PowerShell:

```powershell
git clone https://github.com/dgallagher33/nfl-rosterwire.git
cd nfl-rosterwire
git switch feat/deterministic-roster-sync
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`nflreadpy` supplies the ESPN/GSIS identity cross-reference. ESPN HTTP fetching and offline tests use the Python standard library. No API credentials or subscription are needed.

Place your private Mod Studio export at `roster.csv`. CSVs, downloaded data, reports, virtual environments and common save formats are ignored by Git; never force-add private game assets.

## Fetch, inspect and plan

Use a new snapshot filename and output directory for each run:

```powershell
.\.venv\Scripts\python.exe studio_sync.py inspect --roster roster.csv --out output/inventory.json
.\.venv\Scripts\python.exe studio_sync.py fetch --provider espn --out data/current_nfl.csv
.\.venv\Scripts\python.exe studio_sync.py plan --roster roster.csv --nfl data/current_nfl.csv --out output/week01
```

Keep `data/current_nfl.csv.meta.json` beside the snapshot. The manifest records its hash, all 32 team counts, URLs, original retrieval times and cache use. Retrieval time is not a promise that ESPN has published every transaction. Default HTTP timeout is 20 seconds with three attempts; cached responses expire after 15 minutes and are never used as a stale fallback.

Planning writes:

| File | Purpose |
|---|---|
| `summary.json` | Counts, source provenance, roster/proposal hashes; required for staging |
| `proposed_transactions.csv` | Deterministic MOVE/SIGN proposals, initially PENDING |
| `needs_review.csv` | Missing game-player matches, conflicting IDs, uncertain statuses/releases |
| `missing_players.csv` | Unlinked NFL players, with separate missing-player and unresolved-identity reasons |
| `player_crosswalk.csv` | Persistent slot/name/DOB/GSIS/ESPN links and verification evidence |
| `source_manifest.json` | Copy of source provenance |

Pass the prior crosswalk on later runs, writing to a new directory:

```powershell
.\.venv\Scripts\python.exe studio_sync.py plan --roster roster.csv --nfl data/next_nfl.csv --crosswalk output/week01/player_crosswalk.csv --out output/week02
```

Saved identities are checked against the current slot and current source. Links survive temporary source absence but cannot authorize a move while absent. Reused slots, duplicate IDs or normalized identity collisions require review. Fuzzy/nickname guessing is not implemented.

`plan --dry-run` prints counts without writing reports. Planning never edits a roster. Legacy `fetch --season 2026` remains available, but season or unproven CSV snapshots are inspection-only and cannot authorize staging.

## Approve and stage

Inspect the reports and verify each intended move. Edit **only** `approval` in `proposed_transactions.csv` to `APPROVED`; leave unwanted rows `PENDING` or set `REJECTED`. Do not remove rows or edit destinations, identities or actions. Keep the summary unedited.

```powershell
.\.venv\Scripts\python.exe studio_sync.py stage --roster roster.csv --changes output/week01/proposed_transactions.csv --summary output/week01/summary.json --out output/staged_week01.csv --dry-run
.\.venv\Scripts\python.exe studio_sync.py stage --roster roster.csv --changes output/week01/proposed_transactions.csv --summary output/week01/summary.json --out output/staged_week01.csv
```

Inspect `output/staged_week01.csv.manifest.json` for exact before/after teams, pool counts, column list and hashes. Existing output paths are rejected. Expired data or a changed original requires a fresh fetch/plan/approval cycle. Hashes detect accidental changes, not a malicious rewrite of both proposals and their manifest.

Rollback: retain and continue using the untouched original `roster.csv`; discard the staged copy. Do not import over a working save until the import pathway has been validated on a backup.

## Review-only cases and failures

- Absence from ESPN never proves release. Even a season UFA/RFA label is review-only; no reliable release-evidence adapter exists yet.
- Injured/reserve, suspended, practice-squad and unknown statuses never cause automatic moves. Same-team membership is preserved.
- Ambiguous names/DOBs, missing birthdates, duplicate GSIS/ESPN IDs and cross-source conflicts are withheld. Missing GSIS is explicit; a unique ESPN identity may still match a game name/DOB.
- Unknown team aliases, empty/missing required position groups, fewer than 40 entries on a team, duplicate athletes across teams, or a failed endpoint abort ingestion. The 40-player floor is a conservative completeness check, not a guarantee of a complete roster.
- On schema/network errors, no new successful snapshot is published; retry with a fresh output path after investigating the reported URL. An older snapshot is never silently refreshed.
- `missing_players.csv` does not authorize adding or replacing game records.

## Tests and live smoke check

Offline tests need no installed dependencies, assets, credentials or network:

```powershell
py -m unittest discover -s tests -v
py -m py_compile studio_sync.py nfl_sources.py roster_common.py
```

CI runs these tests on Python 3.10 and 3.13. The suite uses small fictional source fixtures plus generated expanded rosters to verify all 4,164 records and 91 columns.

The explicit `fetch --provider espn` command above is also the live smoke check; it is not part of CI. On November 11, 2026, it retrieved 32 teams / 2,549 players; 2,420 nflverse identity mappings verified, 115 conflicted, and 14 were unmapped. Conflicts were withheld, not resolved by guessing. No private SOFTDRINK export was available in this milestone's workspace; real-game import remains untested.

## Architecture and next step

See [architecture and source schemas](docs/architecture.md) and the earlier [save-tool investigation](docs/save-writer-research.md). Next: validate a no-op expanded CSV import/export through Mod Studio on a backed-up real roster, then one approved team-only move and an xemu load test. Binary save writing remains deferred.
