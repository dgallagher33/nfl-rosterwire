# Live transaction reconciliation

## Scope and module boundaries

`nfl_sources.py` owns the provider protocol, ESPN parsing, HTTP cache/retries,
nflreadpy identity join and snapshot validation. `roster_common.py` owns shared
team/name/date normalization and the 24-hour freshness guard. `studio_sync.py`
retains the existing CLI, CSV planner, persistent crosswalk and approval/staging
workflow. There is no service, database, scheduler, ratings engine or save writer.

## Sources observed on November 11, 2026

- [ESPN team list](https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams):
  `sports[0].leagues[0].teams[].team`, with `id` and `abbreviation`.
- [ESPN Buffalo roster](https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/2/roster):
  `team.abbreviation`, `athletes[].position`, `athletes[].items[]`.
  Athlete fields used: `id`, `fullName`, `dateOfBirth`, `position.abbreviation`,
  `status.type`, `injuries`. The response also has timestamp/season metadata;
  neither is treated as a guaranteed transaction-effective time.
- Observed group names: `offense`, `defense`, `specialTeam`,
  `injuredReserveOrOut`, `suspended`, `practiceSquad`. Reserve and practice groups
  override nominal athlete Active status. Missing/unknown status is not Active.
- [nflreadpy load_players source](https://github.com/nflverse/nflreadpy/blob/main/src/nflreadpy/load_players.py)
  downloads the nflverse player identity table. Version 0.1.5 was exercised.
  Required columns: `gsis_id`, `espn_id`, `display_name`, `birth_date`,
  `latest_team`, `position`. Latest team is identity context only, never live
  transaction evidence. The [nflverse players release](https://github.com/nflverse/nflverse-data/releases/tag/players)
  is recorded as identity provenance. nflreadpy manages its own identity cache;
  the adapter never uses its team field to override ESPN membership.

The canonical CSV preserves provider/athlete IDs, GSIS and ESPN IDs, name,
birthdate, position, canonical team, roster status, UTC retrieval time, source
URL and identity verification state/reason/source. Empty strings mean unavailable.
`status` duplicates `roster_status` for compatibility with the existing planner.

HTTP checks status and JSON shape, uses a 20-second timeout and three attempts
with 1/2-second backoff. Nontransient HTTP errors stop immediately. A 15-minute
cache retains its original retrieval timestamp and records cache hits. Expired
cache entries never become fallback results after failure. All 32 unique teams,
required nonempty position groups, at least 40 players per team, matching team
codes and globally unique ESPN athlete IDs are required before publishing.

The live smoke run retrieved 2,549 athletes across 32 teams. The identity join
verified 2,420 and marked 115 conflicts / 14 unmapped. These are observation
counts, not fixed test expectations. ESPN is an undocumented public API without
a completeness SLA. A partial feed above the completeness floor, an unreported
transaction, or upstream lag cannot be ruled out. Human approval remains required.

## Identity and persistence

New matches require unique normalized full name plus a valid exact calendar
birthdate. Normalization removes punctuation, accents and common trailing suffixes;
it does not guess nicknames. A collision anywhere among eligible game identities
withholds all affected slots. Duplicate source GSIS/ESPN IDs likewise withhold all
affected matches. Cross-source conflicts block matching, even with matching names.

The crosswalk stores pool/index, current slot name/DOB, both NFL IDs, method,
verification state/reason and source URL. A prior link is checked against the
current occupant and source identifiers. Incomplete legacy links require review.
Absent players retain their checked slot identity with `absent_from_source` state;
their membership cannot cause an action. Persist links by passing the previous
run's file via `--crosswalk`; use a fresh report directory to preserve audit history.
No fuzzy match is automatically accepted. An ESPN-only exact match is possible
when nflverse has no matching identity, but not when nflverse contradicts it.

## Deterministic decisions

Active membership differences produce MOVE or SIGN. Same membership produces no
proposal. IR, reserve, practice squad and unknown statuses preserve assignments
or produce review. Feed absence is review-only. UFA/RFA is also review-only:
neither selected source establishes independently verified release events. A
future release provider needs explicit evidence and tests before RELEASE can be
generated. A caller-supplied `release_verified` flag is deliberately insufficient.

Proposal ordering and identity decisions are deterministic for identical inputs;
audit generation timestamps naturally differ. Replanning an applied team change
produces no repeat action. Missing-player reports distinguish no exact eligible
game identity from unresolved identity; neither authorizes creating a player.

## Approval and preservation boundary

The CLI accepts only 4,096 primary / 68 secondary / 91-column exports. The pure
reconciler accepts small fixtures for tests; it cannot itself write a roster.
Other expanded exports require a separately validated profile, not a bypass flag.
Season/unproven snapshots can be inspected but their summaries block staging.

Planning validates the source CSV hash, all team counts and retrieval ages.
The summary binds the original roster SHA-256 and canonical proposal SHA-256;
only approval cells are excluded from that digest. Staging requires the summary,
checks freshness again and validates slot/name/DOB/from-team/action consistency.
Local hashes guard accidental changes, not an adversary editing both data and
manifests. Do not edit the summary to bypass a failure.

The updater retains every row and column in order and checks that exactly the
approved primary `team` cells changed. Secondary, draft and historical records
are excluded. It serializes a temporary file in the output directory, reads it
back and compares every cell, then publishes it with a non-overwriting hard link.
The filesystem must support hard links (for example NTFS or ordinary Linux
filesystems); unsupported filesystems fail rather than silently use an unsafe
overwrite. The temporary name is removed. A manifest lists exact changes, pool
counts, column order and hashes. CSV whitespace/quoting is not byte-preserved.

Rollback consists of continuing to use the unchanged original export. No Mod
Studio, game save, xemu memory card or deployed service is changed by this tool.

## Reference and licensing boundary

[ActiveRosterUpdate](https://github.com/lionsfan60/active-roster-update) was studied
through its ESPN, HTTP, match, model, build and CLI modules. No license was listed
in its repository root during inspection. No implementation was copied. This
project independently implements ingestion and incremental slot updates; it does
not adopt that project's fuzzy approvals, stale-cache fallback, ratings synthesis
or full-team rebuilding.

## Verification and next integration

Normal CI runs offline unittest fixtures on Python 3.10 and 3.13. Live fetching
is separately invoked using `fetch --provider espn`. Expanded tests generate all
4,164 fictional records and 91 columns; no copyrighted exports are committed.
Real SOFTDRINK assets were unavailable in this workspace, so byte-level real
export quirks, Mod Studio import and playable saves remain unverified.

The next milestone is a backed-up no-op Mod Studio CSV import/export comparison,
then a single approved team move and xemu load check. Do not feed this expanded
roster into a conventional 2,317-player writer or implement binary export until
that compatibility boundary has been proven.
