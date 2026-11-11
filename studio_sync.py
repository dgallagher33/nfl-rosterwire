#!/usr/bin/env python3
"""Conservative SOFTDRINK 2K28 exported-roster transaction planner.

Works on the original Mod Studio export CSV's pool/index/team/first/last/
position/birth_date columns. Does not claim a game-save write path.
Only changes the team field after explicit approval and validates every
other field is unchanged. ESPN provides live membership; nflreadpy supplies
identity mappings. Neither source is a verified transaction ledger.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

REQUIRED_GAME = {"pool", "index", "team", "first", "last", "position", "birth_date"}
REQUIRED_NFL = {"gsis_id", "full_name", "birth_date", "team", "status"}
from roster_common import CANONICAL_TEAMS, TEAM_ALIASES, normalize_name, normalize_date, nfl_team, stamp, require_fresh
from nfl_sources import EspnProvider, JsonClient, SOURCE_FIELDS, attach_identities, load_identities, validate_snapshot

MEMBERSHIP_CODES = set(CANONICAL_TEAMS)
ACTIVE_SOURCE_STATUSES = {"ACT", "INA"}
HOLD_SOURCE_STATUSES = {"RES", "DEV"}
FREE_AGENT_SOURCE_STATUSES = {"UFA", "RFA"}
CHANGE_FIELDS = ["pool", "index", "player", "position", "birth_date", "gsis_id", "from_team", "to_team", "action", "nfl_status", "match_method", "approval"]
REVIEW_FIELDS = ["pool", "index", "player", "position", "birth_date", "game_team", "reason", "candidates"]
LINK_FIELDS = ["pool", "index", "gsis_id", "player", "birth_date", "match_method", "reviewed", "espn_id", "verification_state", "verification_reason", "source_url"]


def load_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f"Missing or duplicate CSV header: {path}")
        rows = list(reader)
        if any(None in r or any(v is None for v in r.values()) for r in rows):
            raise ValueError(f"Unexpected extra comma/columns in {path}")
        return rows, list(reader.fieldnames)


def save_csv(path, rows, fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="raise", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def save_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def get_key(row):
    return (row["pool"].strip(), row["index"].strip())


def display(row):
    return (row["first"].strip() + " " + row["last"].strip()).strip()


def valid_player(row):
    return (row["pool"] == "primary" and row["team"] not in {"pool", "draft_class"} and
            row["first"].strip() not in {"", "****************"} and
            row["last"].strip() not in {"", "****************"})


def validate_game(rows, fields):
    missing = REQUIRED_GAME - set(fields)
    if missing:
        raise ValueError(f"Missing SOFTDRINK roster columns: {sorted(missing)}")
    keys = [get_key(r) for r in rows]
    if len(set(keys)) != len(keys) or any(not all(k) for k in keys):
        raise ValueError("Duplicate or blank (pool,index) pairs")
    if not all(r["pool"] in {"primary", "secondary"} for r in rows):
        raise ValueError("Unexpected pool type")
    for r in rows:
        if not r["index"].isdigit():
            raise ValueError("Non-numeric player index")
        team = r["team"]
        # Historical / special teams must stay untouched.
        if not team or (team.startswith("reserve:") and not team[8:]):
            raise ValueError(f"Blank/invalid team value {team!r} for {get_key(r)}")
    return keys


def membership(team):
    if team in MEMBERSHIP_CODES:
        return team
    if team.startswith("reserve:") and team[8:] in MEMBERSHIP_CODES:
        return team[8:]
    if team == "free_agent":
        return "free_agent"
    return None


def validate_expanded(rows, fields):
    validate_game(rows, fields)
    if Counter(r["pool"] for r in rows) != {"primary": 4096, "secondary": 68} or len(fields) != 91:
        raise ValueError("Expected expanded SOFTDRINK export: 4096 primary, 68 secondary, 91 columns")


def read_links(path):
    if not path:
        return {}
    rows, fields = load_csv(path)
    if not {"pool", "index", "gsis_id"} <= set(fields):
        raise ValueError("Crosswalk needs pool,index,gsis_id columns")
    result = {}
    for row in rows:
        key = get_key(row)
        if key in result:
            raise ValueError(f"Duplicated crosswalk key {key}")
        if row.get("gsis_id", "").strip() or row.get("espn_id", "").strip():
            result[key] = row
    for field in ("gsis_id", "espn_id"):
        ids = [r.get(field) for r in result.values() if r.get(field)]
        if len(set(ids)) != len(ids):
            raise ValueError(f"Same {field} linked to multiple game slots")
    return result


def make_plan(game, source, crosswalk=None):
    validate_game(game, list(game[0]) if game else [])
    if not source:
        raise ValueError("NFL source has no rows")
    if not REQUIRED_NFL <= set(source[0]):
        raise ValueError(f"NFL data missing {sorted(REQUIRED_NFL - set(source[0]))}")
    by_person = defaultdict(list)
    by_id = defaultdict(list)
    by_espn = defaultdict(list)
    source_counts = {field: Counter(r.get(field) for r in source if r.get(field))
                     for field in ("gsis_id", "espn_id")}
    game_counts = Counter((normalize_name(display(r)), normalize_date(r["birth_date"]))
                          for r in game if valid_player(r) and membership(r["team"]) is not None)
    for r in source:
        d = normalize_date(r["birth_date"])
        nm = normalize_name(r["full_name"])
        if d and nm:
            by_person[(nm, d)].append(r)
        if r["gsis_id"]:
            by_id[r["gsis_id"]].append(r)
        if r.get("espn_id"):
            by_espn[r["espn_id"]].append(r)
    crosswalk = crosswalk or {}
    proposed, needs_review, linked = [], [], []
    linked_ids = set()

    def issue(row, reason, candidates=""):
        needs_review.append({"pool":row["pool"], "index":row["index"], "player":display(row),
                             "position":row["position"], "birth_date":normalize_date(row["birth_date"]),
                             "game_team":row["team"], "reason":reason, "candidates":candidates})

    for r in game:
        if not valid_player(r):
            continue
        current = r["team"]
        if membership(current) is None:
            continue
        nm = display(r)
        birth = normalize_date(r["birth_date"])
        if not birth:
            issue(r, "No valid birth date; cannot uniquely match")
            continue
        if game_counts[(normalize_name(nm), birth)] != 1:
            issue(r, "Duplicate game identity; all colliding slots withheld")
            continue
        if len(by_person.get((normalize_name(nm), birth), [])) > 1:
            issue(r, "Duplicate NFL identity matches; verify source records")
            continue
        saved = crosswalk.get(get_key(r))
        if saved:
            if not saved.get("player") or not normalize_date(saved.get("birth_date")):
                issue(r, "Crosswalk lacks slot identity evidence; verify")
                continue
            # A crosswalk must not silently survive a changed player in a reused slot.
            if saved.get("player") and normalize_name(saved["player"]) != normalize_name(nm):
                issue(r, "Crosswalk player name differs from current slot; verify")
                continue
            if saved.get("birth_date") and normalize_date(saved["birth_date"]) != birth:
                issue(r, "Crosswalk birth date differs from current slot; verify")
                continue
            candidates = by_id.get(saved["gsis_id"], []) if saved.get("gsis_id") else by_espn.get(saved.get("espn_id"), [])
            match_method = saved.get("match_method", "crosswalk") or "crosswalk"
        else:
            candidates = by_person.get((normalize_name(nm), birth), [])
            match_method = "exact_name_birth_date"
        if len(candidates) != 1:
            if saved and not candidates:
                # Keep an identity through temporary feed absence, but never act on it.
                retained = {field: saved.get(field, "") for field in LINK_FIELDS}
                retained["verification_state"] = "absent_from_source"
                retained["verification_reason"] = "Slot identity unchanged; source membership unavailable"
                linked.append(retained)
            issue(r, "No unique NFL match (or missing from feed)" if not candidates else "Duplicate NFL identity matches",
                  "; ".join(f'{p["gsis_id"]} {p["full_name"]}' for p in candidates[:5]))
            continue
        n = candidates[0]
        if (n.get("identity_state") == "conflict" or
                any(n.get(f) and source_counts[f][n[f]] != 1 for f in source_counts) or
                (saved and any(saved.get(f) and n.get(f) != saved[f] for f in source_counts))):
            issue(r, "Conflicting or duplicate NFL identifiers")
            continue
        pid = n["gsis_id"].strip() or ("espn:" + n.get("espn_id", ""))
        if pid == "espn:":
            issue(r, "No stable NFL identifier")
            continue
        if pid in linked_ids:
            issue(r, "Same NFL player already linked to another game slot", pid)
            continue
        if normalize_date(n["birth_date"]) != birth or normalize_name(n["full_name"]) != normalize_name(nm):
            issue(r, "Crosswalk GSIS ID conflicts with name or DOB", pid)
            continue
        linked_ids.add(pid)
        linked.append({"pool":r["pool"], "index":r["index"], "gsis_id":n["gsis_id"],
                       "espn_id":n.get("espn_id", ""), "verification_state":"verified",
                       "verification_reason":"Unique name and birthdate; identifiers consistent",
                       "source_url":n.get("source_url", ""),
                       "player":nm, "birth_date":birth, "match_method":match_method,
                       "reviewed":saved.get("reviewed", "NO") if saved else "NO"})
        current_member = membership(current)
        source_status = n["status"].strip().upper()
        source_team = nfl_team(n["team"])
        if source_status in ACTIVE_SOURCE_STATUSES | HOLD_SOURCE_STATUSES:
            if not source_team:
                issue(r, f"Unknown NFL team {n['team']!r}", pid)
                continue
            if source_team == current_member:
                continue
            if source_status in HOLD_SOURCE_STATUSES:
                issue(r, f"Team change with status {source_status}: reserve/practice squad requires manual handling", pid)
                continue
            dest = source_team
        elif source_status in FREE_AGENT_SOURCE_STATUSES:
            issue(r, "Potential release: free-agent status needs independent transaction evidence", pid)
            continue
        else:
            issue(r, f"Unknown or ambiguous NFL status {source_status!r}", pid)
            continue
        if dest == current_member:
            continue
        if dest == "free_agent":
            if current_member == "free_agent":
                continue
            action = "RELEASE"
        elif current_member == "free_agent":
            action = "SIGN"
        else:
            action = "MOVE"
        proposed.append({"pool":r["pool"], "index":r["index"], "player":nm,
                         "position":r["position"], "birth_date":birth, "gsis_id":n["gsis_id"],
                         "from_team":current, "to_team":dest, "action":action,
                         "nfl_status":source_status, "match_method":match_method,
                         "approval":"PENDING"})
    proposed.sort(key=lambda r: (r["action"], r["player"], int(r["index"])))
    needs_review.sort(key=lambda r: (r["reason"], r["player"], int(r["index"])))
    linked.sort(key=lambda r: int(r["index"]))
    return proposed, needs_review, linked


def inventory(roster, source=None):
    rows, fields = load_csv(roster)
    validate_expanded(rows, fields)
    pools = Counter(r["pool"] for r in rows)
    teams = Counter(r["team"] for r in rows)
    active = [r for r in rows if valid_player(r)]
    return {"roster_path":str(roster), "sha256":digest(roster), "records":len(rows),
            "columns":len(fields), "pools":dict(pools), "teams":dict(sorted(teams.items())),
            "eligible_named_primary_records":len(active), "eligible_with_valid_dob":sum(bool(normalize_date(r["birth_date"])) for r in active),
            "noncurrent_teams":dict(sorted(Counter(r["team"] for r in rows if r["team"] not in MEMBERSHIP_CODES and r["team"] not in {"free_agent", "draft_class", "pool"} and not r["team"].startswith("reserve:")).items())),
            "source_compatibility":"91-column format is exported; import/writeback not proven"}


def fetch(season, out):
    if Path(out).exists() or Path(str(out) + ".meta.json").exists():
        raise ValueError("Snapshot already exists; choose a fresh path")
    try:
        import nflreadpy as nfl
    except ImportError as e:
        raise RuntimeError("Install nflreadpy: py -m pip install nflreadpy") from e
    frame = nfl.load_rosters(season)
    columns = ["gsis_id", "full_name", "birth_date", "team", "status", "position"]
    if not set(columns) <= set(frame.columns):
        raise ValueError(f"nflreadpy source schema changed: missing {set(columns)-set(frame.columns)}")
    source = []
    for item in frame.select(columns).to_dicts():
        row = {c: str(item[c]) if item[c] is not None else "" for c in columns}
        if row["gsis_id"].strip():
            source.append(row)
    ids = [r["gsis_id"] for r in source]
    if len(ids) != len(set(ids)):
        raise ValueError("NFL source has duplicate gsis_id; stop and inspect season data")
    source.sort(key=lambda r:r["gsis_id"])
    save_csv(out, source, columns)
    save_json(str(out)+".meta.json", {"season":season,"fetched_at_utc":stamp(),
              "rows":len(source),"source":"nflreadpy.load_rosters",
              "warning":"Fetch time does NOT confirm upstream data freshness"})
    print(f"Fetched {len(source)} roster rows to {out}; verify upstream freshness")


def proposal_digest(rows):
    canonical = [{k: row[k] for k in CHANGE_FIELDS if k != "approval"} for row in rows]
    canonical.sort(key=lambda r: (r["pool"], r["index"]))
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def fetch_live(out, cache_dir=None):
    if Path(out).exists() or Path(str(out) + ".meta.json").exists():
        raise ValueError("Snapshot already exists; choose a fresh path")
    identities = load_identities()
    source, manifest = EspnProvider(JsonClient(cache_dir)).fetch()
    attach_identities(source, identities)
    validate_snapshot(source, manifest)
    save_csv(out, source, SOURCE_FIELDS)
    manifest["sha256"] = digest(out)
    manifest["identity_source"] = "nflreadpy.load_players"
    manifest["identity_counts"] = dict(Counter(r["identity_state"] for r in source))
    save_json(str(out) + ".meta.json", manifest)
    print(f"NFL teams retrieved: 32; NFL players retrieved: {len(source)}")
    print(f"Identity mapping: {manifest['identity_counts']}; fetched: {manifest['fetched_at']}")
    return manifest


def plan_paths(roster, nfl, map_path, out, dry_run=False):
    game, fields = load_csv(roster)
    validate_expanded(game, fields)
    source, nfl_fields = load_csv(nfl)
    if not REQUIRED_NFL <= set(nfl_fields):
        raise ValueError(f"NFL source requires {sorted(REQUIRED_NFL)}")
    manifest_path = Path(str(nfl) + ".meta.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    live = manifest.get("provider") == "espn"
    if live:
        if manifest.get("sha256") != digest(nfl):
            raise ValueError("Snapshot hash mismatch")
        validate_snapshot(source, manifest)
    elif any(r.get("provider") == "espn" for r in source):
        raise ValueError("ESPN snapshot requires its complete source manifest")
    proposals, reviews, links = make_plan(game, source, read_links(map_path))
    linked_ids = {(r["gsis_id"], r.get("espn_id", "")) for r in links}
    game_people = {(normalize_name(display(r)), normalize_date(r["birth_date"]))
                   for r in game if valid_player(r) and membership(r["team"]) is not None}
    missing = []
    for record in source:
        if (record["gsis_id"], record.get("espn_id", "")) in linked_ids:
            continue
        key = (normalize_name(record["full_name"]), normalize_date(record["birth_date"]))
        reason = "REVIEW: identity unresolved" if key in game_people or not key[1] or record.get("identity_state") == "conflict" else "MISSING PLAYER: no exact eligible game identity"
        missing.append({**record, "reason": reason})
    # Historical or unproven feeds may be inspected but can never authorize staging.
    metadata = {"generated_at_utc":stamp(), "game_sha256":digest(roster),
                "nfl_sha256":digest(nfl), "game_rows":len(game), "nfl_rows":len(source),
                "matched_players":len(links), "proposals":len(proposals), "reviews":len(reviews),
                "missing_players":sum(r["reason"].startswith("MISSING") for r in missing),
                "unresolved_source_players":len(missing),
                "identity_conflicts":sum(r.get("identity_state") == "conflict" for r in source),
                "potential_releases_needing_review":sum("Potential release" in r["reason"] or "missing from feed" in r["reason"] for r in reviews),
                "verified_identities":sum(r["verification_state"] == "verified" for r in links),
                "actions":dict(Counter(r['action'] for r in proposals)),
                "source_verified":live, "source_manifest":manifest,
                "proposal_sha256":proposal_digest(proposals),
                "caveats":["Explicit approval required", "No release evidence adapter: releases review-only",
                           "Season snapshots are inspection-only", "Retrieval time is not a transaction guarantee"]}
    if not dry_run:
        out = Path(out)
        if out.resolve() in {Path(roster).resolve(), Path(nfl).resolve()}:
            raise ValueError("Report directory overlaps an input")
        outputs = [out / name for name in ("proposed_transactions.csv", "needs_review.csv",
                   "player_crosswalk.csv", "missing_players.csv", "source_manifest.json", "summary.json")]
        if any(p.exists() for p in outputs):
            raise ValueError("Report files already exist; choose a fresh output directory")
        save_csv(outputs[0], proposals, CHANGE_FIELDS)
        save_csv(outputs[1], reviews, REVIEW_FIELDS)
        save_csv(outputs[2], links, LINK_FIELDS)
        save_csv(outputs[3], missing, nfl_fields + ["reason"])
        save_json(outputs[4], manifest or {"warning":"No current source provenance"})
        save_json(outputs[5], metadata)
    print(f"NFL teams retrieved: {len(manifest.get('teams', {}))}; NFL players retrieved: {len(source)}")
    print(f"Game players evaluated: {sum(valid_player(r) and membership(r['team']) is not None for r in game)}")
    print(f"Verified identities: {metadata['verified_identities']}; proposed actions: {metadata['actions']}; review: {len(reviews)}; missing/unresolved players: {len(missing)}")
    print(f"Identity conflicts: {metadata['identity_conflicts']}; potential releases needing review: {metadata['potential_releases_needing_review']}")
    print(f"Source freshness: {manifest.get('fetched_at', 'UNVERIFIED; staging blocked')}")
    return metadata


def stage(roster, changes, out, summary=None, dry_run=False):
    src, fields = load_csv(roster)
    validate_expanded(src, fields)
    original_hash = digest(roster)
    if not summary:
        raise ValueError("Plan summary is required for staging")
    if summary:
        meta = json.loads(Path(summary).read_text(encoding="utf-8"))
        if meta.get("game_sha256") != original_hash:
            raise ValueError("The roster file changed after planning; re-run plan")
    if meta.get("source_verified") is not True:
        raise ValueError("Unverified/season source cannot authorize staging; fetch --provider espn")
    require_fresh(meta.get("source_manifest", {}).get("fetched_at"))
    planned, columns = load_csv(changes)
    if not set(CHANGE_FIELDS) <= set(columns):
        raise ValueError("Transaction CSV missing required fields")
    if proposal_digest(planned) != meta.get("proposal_sha256"):
        raise ValueError("Proposal contents changed; only the approval column may be edited")
    if Path(out).resolve() in {Path(roster).resolve(), Path(changes).resolve(), Path(summary).resolve()}:
        raise ValueError("Refusing to overwrite the source roster")
    if Path(out).exists() or Path(str(out) + ".manifest.json").exists():
        raise ValueError("Output already exists; choose a fresh output path")
    original_by_key = {get_key(r):r for r in src}
    approved = {}
    for c in planned:
        approval = c["approval"].strip().upper()
        if approval not in {"PENDING","APPROVED","REJECTED"}:
            raise ValueError(f"Invalid approval: {approval!r}")
        if approval != "APPROVED":
            continue
        key = get_key(c)
        if key in approved:
            raise ValueError(f"Duplicate approved changes for {key}")
        if key not in original_by_key:
            raise ValueError(f"Player slot no longer exists: {key}")
        r = original_by_key[key]
        if not valid_player(r) or membership(r["team"]) is None or r["team"] != c["from_team"] or display(r) != c["player"] or normalize_date(r["birth_date"]) != c["birth_date"]:
            raise ValueError(f"Player changed or source team mismatch: {key}")
        target = c["to_team"]
        if target not in MEMBERSHIP_CODES | {"free_agent"}:
            raise ValueError(f"Unsupported destination: {target}")
        if c["action"] not in {"SIGN", "RELEASE", "MOVE"}:
            raise ValueError("Unknown action")
        expected_action = "RELEASE" if target == "free_agent" else "SIGN" if membership(r["team"]) == "free_agent" else "MOVE"
        if c["action"] != expected_action or r["team"] == target:
            raise ValueError(f"Action/from/to inconsistent for {key}")
        approved[key] = c
    if not approved:
        raise ValueError("No APPROVED transactions; unchanged roster not written")
    updated = []
    for r in src:
        updated_row = dict(r)
        c = approved.get(get_key(r))
        if c:
            updated_row["team"] = c["to_team"]
        updated.append(updated_row)
    for before, after in zip(src, updated, strict=True):
        key = get_key(before)
        changed = {k for k in fields if before[k] != after[k]}
        if changed != ({"team"} if key in approved else set()):
            raise ValueError(f"Unexpected changed fields at {key}")
    validate_expanded(updated, fields)
    if dry_run:
        print(f"Dry run: {len(approved)} approved team-only changes validated; no output written")
        return {"approved_changes": len(approved), "dry_run": True}
    # Validate the serialized CSV before publishing; exclusive output creation never overwrites.
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=out.parent, suffix=".tmp")
    os.close(fd)
    temp = Path(temp_name)
    try:
        save_csv(temp, updated, fields)
        emitted, emitted_fields = load_csv(temp)
        if emitted_fields != fields or emitted != updated:
            raise ValueError("Written CSV did not round-trip exactly")
        if digest(roster) != original_hash:
            raise ValueError("Source roster changed during staging")
        # Same-directory hard link publishes the validated bytes atomically and fails
        # if the destination exists. Removing temp below leaves the output intact.
        os.link(temp, out)
    finally:
        temp.unlink(missing_ok=True)
    report = {"created_at_utc":stamp(),"source_sha256":original_hash,
              "staged_sha256":digest(out),"records":len(updated),"approved_changes":len(approved),
              "changed_columns":["team"], "pools":dict(Counter(r["pool"] for r in updated)),
              "columns": fields, "changes":list(approved.values()),"actions":dict(Counter(c['action'] for c in approved.values())),
              "warning":"This is a staged CSV only. Re-import into Mod Studio and verify in xemu before using."}
    try:
        with Path(str(out) + ".manifest.json").open("x", encoding="utf-8") as manifest_file:
            json.dump(report, manifest_file, indent=2, sort_keys=True)
    except OSError:
        out.unlink(missing_ok=True)
        raise
    print(f"Staged {len(approved)} approved team-only updates at {out}; import pathway remains unverified")
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd",required=True)
    i = sub.add_parser("inspect", help="Summarize an untouched Mod Studio roster export")
    i.add_argument("--roster",type=Path,required=True)
    i.add_argument("--out",type=Path)
    f = sub.add_parser("fetch",help="Fetch live ESPN membership or a historical nflreadpy snapshot")
    f.add_argument("--provider", choices=["espn", "nflreadpy"], default="nflreadpy")
    f.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    f.add_argument("--season",type=int,default=2026)
    f.add_argument("--out",type=Path,default=Path("data/nfl_roster_2026.csv"))
    pl = sub.add_parser("plan",help="Generate transaction proposals from exact identities")
    pl.add_argument("--dry-run", action="store_true")
    pl.add_argument("--roster",type=Path,required=True)
    pl.add_argument("--nfl",type=Path,required=True)
    pl.add_argument("--crosswalk",type=Path)
    pl.add_argument("--out",type=Path,default=Path("output"))
    a = sub.add_parser("stage",help="Stage only explicitly APPROVED team changes in a copied CSV")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--roster",type=Path,required=True)
    a.add_argument("--changes",type=Path,required=True)
    a.add_argument("--summary",type=Path,required=True,help="Required plan summary for source and proposal integrity")
    a.add_argument("--out",type=Path,required=True)
    args=p.parse_args()
    if args.cmd == "inspect":
        result=inventory(args.roster)
        if args.out:
            if args.out.exists() or args.out.resolve() == args.roster.resolve():
                raise ValueError("Inspection output already exists or overlaps roster")
            save_json(args.out,result)
        print(json.dumps(result,indent=2))
    elif args.cmd == "fetch":
        if args.provider == "espn":fetch_live(args.out,args.cache_dir)
        else:fetch(args.season,args.out)
    elif args.cmd == "plan":plan_paths(args.roster,args.nfl,args.crosswalk,args.out,args.dry_run)
    else:stage(args.roster,args.changes,args.out,args.summary,args.dry_run)


if __name__ == "__main__":
    main()
