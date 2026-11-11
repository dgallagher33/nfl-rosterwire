# Save-writer research: BAD-AL's NFL2K5Tool

Status: **candidate integration, NOT verified against SOFTDRINK 2K28's expanded roster**.

This document records why RosterWire should separate deterministic transaction detection from output adapters.

## Related projects

- C# desktop application: https://github.com/BAD-AL/NFL2K5Tool
- Dart CLI and library: https://github.com/BAD-AL/nfl2k5tool_dart
- Browser-based save editor: https://github.com/BAD-AL/nfl2k5tool_web

**What works upstream (per project documentation):**
- Import Xbox save bundles (\`.zip\`), raw \`SAVEGAME.DAT\`, Xbox memory-unit images (\`.bin\`/\`.img\`).
- Export to Xbox roster bundles and memory-unit images with platform-specific signing.
- Apply text-form player edits via Dart CLI:
  \`nfl2k5tool [save_file] [data_to_apply.txt] [options]\`.
- \`-out:<file>\` exports modified save, \`-audc\` recalculates depth charts. Do not use \`-audc\` until validated against the mod's custom layouts.
- Dart library \`SaveSession\` exposes \`fromXboxZip(...)\`, \`engine\`, \`InputParser.ProcessText(...)\`, and \`exportToXboxZip()\`.

**Critical compatibility gap:** The Dart implementation's \`lib/gamesave_tool.dart\` uses an \`mMaxPlayers\` limit of **2317** (and \`FirstDraftClassPlayer=1937\`), with hard-coded offsets. Our actual SOFTDRINK Mod Studio CSV has **4096 primary and 68 secondary records**. The source code does not establish support for the expanded file format. A tool's ability to load/write traditional NFL 2K5 saves or compatible community files does **not** prove it can round-trip this mod's expanded save.

**Second implementation constraint:** \`lib/input_parser.dart\` processes \`TEAM = ...\` sections and sequential player lines by **team list order**, or \`LookupAndModify\` by player name/position. The latter writes attributes for the first matching player. Neither is verified as a safe, direct change to team membership: team ownership is represented via team player lists/pointers. Never synthesize a partial player CSV script and assume it performs an atomic transfer; it could overwrite the wrong player or fail to move membership.

**Original C# warning:** Its README cautions about name-sharing in saves modified using Flying Finn's editor; extract player data and apply to a clean save rather than modifying such an existing save in place.

## Recommended adapter contract

\`RosterWire\` is source-format agnostic at the planning layer:

1. **Input**: immutable original Mod Studio CSV (or a future BAD-AL save extractor), NFL snapshot + approved identity crosswalk.
2. **Planner**: \`proposed_transactions.csv\` and \`needs_review.csv\`, stable source hashes and conservative statuses.
3. **Approval**: human signs off on exact player, from team, to team; draft-class/historical/reserve membership is not automatically changed.
4. **Adapter**: validated backend, e.g. \`studio_csv\` first or \`badal_xbox_zip\` after a proven round trip.
5. **Verification**: export must be re-openable; one-player move only; unchanged players and attribute values byte-/record-equivalent; xemu must load cleanly.
6. **Publication**: versioned output in a **local** output folder; never overwrite the source or automatically replace an active xemu memory card.

Do not mark a writer production-ready until all checks pass.

## Exact proof-of-concept protocol

1. Preserve a **backup** of a working **SOFTDRINK 2K28** roster save in Xbox ZIP form (or an unmodified xemu MU image). Never commit it to GitHub.
2. Try opening it with BAD-AL's latest web or Dart tool **without modifying anything**, then export to a different filename.
3. Open the exported copy again. Compare player counts and key player identities, team memberships, ratings, draft classes and the mod's unique fields.
4. Load the no-op export in xemu, confirming it behaves identically to the original.
5. Make **one** known free-agent/team roster transfer in the editor, export a fresh file and compare the before/after roster. Confirm that only intended membership/depth-list references changed.
6. Load the one-change roster in xemu. Confirm team roster, depth charts, and save/reload.
7. Only then consider implementing a Dart adapter; use a dedicated fixture that is licensed for redistribution, or keep test saves outside the public repo.

If the tool fails, use the Mod Studio native roster/project API for SOFTDRINK saves; retain BAD-AL integration for conventional \`.zip\` and \`.DAT\` saves, if separately validated.

## User-provided evidence

User reports that roster files made with BAD-AL's tool load successfully. This is valuable **game compatibility evidence** for those files, but not a guarantee that applying changes to the specific 4,096-primary/68-secondary SOFTDRINK export is safe.

References checked 2026-11-11:
- https://github.com/BAD-AL/nfl2k5tool_dart/blob/main/README.md
- https://github.com/BAD-AL/nfl2k5tool_dart/blob/main/lib/gamesave_tool.dart
- https://github.com/BAD-AL/nfl2k5tool_dart/blob/main/lib/input_parser.dart
- https://github.com/BAD-AL/NFL2K5Tool/blob/master/README.md
- https://github.com/cruuz/2k-football-mod-tools/blob/main/STATUS.md
