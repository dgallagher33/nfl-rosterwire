# NFL RosterWire

Deterministic weekly transaction planner for **SOFTDRINK NFL 2K28** (ESPN NFL 2K5). This project intentionally starts with ordinary signings, releases and team moves—**not** ratings or game AI.

> **Experimental**: generates transaction proposals and a staged *CSV*, **not** a game-compatible save. Import and game-load testing is still required. Do not overwrite your working roster/save.

## What's implemented

- Reads the actual **91-column Mod Studio exported CSV** (4,096 primary + 68 secondary records).
- Compares that roster to a 2026 NFL roster snapshot from \`nflreadpy\`.
- Matches players on **unique normalized full name + exact date of birth**; produces a GSIS crosswalk. It **never guesses** when identity is ambiguous or missing.
- Proposes \`SIGN\`, \`RELEASE\`, and \`MOVE\` transactions; treats practice squad, reserve, unknown statuses and special/historical teams conservatively.
- Stages **only manually APPROVED** proposals; changes **only the \`team\` column** and preserves every other cell and row.
- Refuses overwriting the original, checks input SHA-256 when staging and emits a manifest and review report.

**Important:** a season snapshot is not an event-by-event transaction ledger. Missing NFL data is **never** treated as proof of release, and exact source freshness should be checked before approving moves.

## Quick start (Windows PowerShell)

Requires Python 3.10+.

Clone the repository:

\`\`\`powershell
git clone https://github.com/dgallagher33/nfl-rosterwire.git
cd nfl-rosterwire
\`\`\`

Place your **private** Mod Studio export at \`roster.csv\` in this directory. The file is excluded via \`.gitignore\`. Never publish copyrighted roster data or personal save files.

Inspect the format:

\`\`\`powershell
py studio_sync.py inspect --roster roster.csv --out output/inventory.json
\`\`\`

Fetch league data:

\`\`\`powershell
py -m pip install nflreadpy
py studio_sync.py fetch --season 2026 --out data/nfl_2026.csv
\`\`\`

Generate a proposal (will not touch your roster):

\`\`\`powershell
py studio_sync.py plan --roster roster.csv --nfl data/nfl_2026.csv --out output
\`\`\`

Inspect \`output/summary.json\`, \`output/needs_review.csv\` and \`output/proposed_transactions.csv\`. Set \`approval\` from \`PENDING\` to \`APPROVED\` only for changes you have verified. The unreviewed player crosswalk can be used on later runs with \`--crosswalk output/player_crosswalk.csv\` **after manual review**.

Stage a new CSV, leaving the original untouched:

\`\`\`powershell
py studio_sync.py stage --roster roster.csv --changes output/proposed_transactions.csv --summary output/summary.json --out output/staged_roster.csv
\`\`\`

If a staged CSV already exists, use a new output name—intentional fail-safe.

## Tests

\`\`\`powershell
py -m unittest discover -s tests -v
\`\`\`

Tests use **fictional** players, not real NFL roster data. No network access required.

## Save-writer candidates

We are evaluating both:

- [2K5 Mod Studio](https://github.com/cruuz/2k-football-mod-tools) for the actual SOFTDRINK expanded roster.
- [BAD-AL NFL2K5Tool Dart](https://github.com/BAD-AL/nfl2k5tool_dart) for a programmatic Xbox ZIP / memory-unit export path.

BAD-AL's tool can extract, edit, sign and export conventional NFL 2K5 saves. **However**, its Dart engine currently contains a 2,317-player limit and fixed offsets; SOFTDRINK v0.9 has a 4,096-player expanded roster. Its input parser is also team-list-order based, not a verified atomic transaction API. We must verify a **no-op round trip** and a **single-player transfer** on a backup before generating Xbox saves.

See [save-writer investigation and validation protocol](docs/save-writer-research.md). No binary save writer is currently implemented here.

## Planned next steps

1. Get an actual working **BAD-AL-generated Xbox roster ZIP** and verify it can be read/re-exported without damage.
2. Independently test the expanded SOFTDRINK save with the Dart tool and with Mod Studio; identify which adapter truly supports moves.
3. Implement just that verified exporter and add a game-load test in xemu.
4. Schedule a Tuesday weekly pipeline (fetch → plan → review → export) through n8n or GitHub Actions **only after safe output is proven**.

Related upstream data: https://nflreadpy.nflverse.com/
