#!/usr/bin/env python3
"""Conservative SOFTDRINK 2K28 exported-roster transaction planner.

Works on the original Mod Studio export CSV's pool/index/team/first/last/
position/birth_date columns. Does not claim a game-save write path.
Only changes the team field after explicit approval and validates every
other field is unchanged. Public roster data fetched via nflreadpy is
season-roster data, not a verified real-time transaction ledger.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_GAME = {"pool", "index", "team", "first", "last", "position", "birth_date"}
REQUIRED_NFL = {"gsis_id", "full_name", "birth_date", "team", "status"}
CANONICAL_TEAMS = "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LAR LAC LV MIA MIN NE NO NYG NYJ PHI PIT SEA SF TB TEN WAS".split()
TEAM_ALIASES = {x:x for x in CANONICAL_TEAMS}
TEAM_ALIASES.update({"JAC":"JAX", "LA":"LAR", "WSH":"WAS", "ARZ":"ARI", "SD":"LAC", "STL":"LAR", "OAK":"LV"})
MEMBERSHIP_CODES = set(CANONICAL_TEAMS)
ACTIVE_SOURCE_STATUSES = {"ACT", "INA"}
HOLD_SOURCE_STATUSES = {"RES", "DEV"}
FREE_AGENT_SOURCE_STATUSES = {"UFA", "RFA"}
CHANGE_FIELDS = ["pool", "index", "player", "position", "birth_date", "gsis_id", "from_team", "to_team", "action", "nfl_status", "match_method", "approval"]
REVIEW_FIELDS = ["pool", "index", "player", "position", "birth_date", "game_team", "reason", "candidates"]
LINK_FIELDS = ["pool", "index", "gsis_id", "player", "birth_date", "match_method", "reviewed"]


def load_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f"Missing or duplicate CSV header: {path}")
        rows = list(reader)
        if any(None in r for r in rows):
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


def stamp():
    return datetime.now(timezone.utc).isoformat()


def get_key(row):
    return (row["pool"].strip(), row["index"].strip())


def display(row):
    return (row["first"].strip() + " " + row["last"].strip()).strip()


def normalize_name(name):
    # Works for T.J. / TJ / TJ, apostrophes and accented characters.
    name = unicodedata.normalize("NFKD", str(name)).casefold()
    return "".join(c for c in name if c.isalnum() and not unicodedata.combining(c))


def normalize_date(date):
    date = str(date or "").strip().lstrip("'")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return date
    return ""


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


def nfl_team(team):
    return TEAM_ALIASES.get(str(team).strip().upper())


def read_links(path):
    if not path or not Path(path).exists():
        return {}
    rows, fields = load_csv(path)
    if not {"pool", "index", "gsis_id"} <= set(fields):
        raise ValueError("Crosswalk needs pool,index,gsis_id columns")
    result = {}
    for row in rows:
        key = get_key(row)
        if key in result:
            raise ValueError(f"Duplicated crosswalk key {key}")
        if row.get("gsis_id", "").strip():
            result[key] = row
    gsis = [r["gsis_id"] for r in result.values()]
    if len(set(gsis)) != len(gsis):
        raise ValueError("Same GSIS ID linked to multiple game slots")
    return result


def make_plan(game, source, crosswalk=None):
    validate_game(game, list(game[0]) if game else [])
    if not source:
        raise ValueError("NFL source has no rows")
    if not REQUIRED_NFL <= set(source[0]):
        raise ValueError(f"NFL data missing {sorted(REQUIRED_NFL - set(source[0]))}")
    by_person = defaultdict(list)
    by_id = defaultdict(list)
    for r in source:
        pid = r["gsis_id"].strip()
        if not pid:
            continue
        d = normalize_date(r["birth_date"])
        nm = normalize_name(r["full_name"])
        if d and nm:
            by_person[(nm, d)].append(r)
        by_id[pid].append(r)
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
        saved = crosswalk.get(get_key(r))
        if saved:
            # A crosswalk must not silently survive a changed player in a reused slot.
            if saved.get("player") and normalize_name(saved["player"]) != normalize_name(nm):
                issue(r, "Crosswalk player name differs from current slot; verify")
                continue
            if saved.get("birth_date") and normalize_date(saved["birth_date"]) != birth:
                issue(r, "Crosswalk birth date differs from current slot; verify")
                continue
            candidates = by_id.get(saved["gsis_id"], [])
            match_method = saved.get("match_method", "crosswalk") or "crosswalk"
        else:
            candidates = by_person.get((normalize_name(nm), birth), [])
            match_method = "exact_name_birth_date"
        if len(candidates) != 1:
            issue(r, "No unique NFL match (or missing from feed)" if not candidates else "Duplicate NFL identity matches",
                  "; ".join(f'{p["gsis_id"]} {p["full_name"]}' for p in candidates[:5]))
            continue
        n = candidates[0]
        pid = n["gsis_id"].strip()
        if pid in linked_ids:
            issue(r, "Same NFL player already linked to another game slot", pid)
            continue
        if normalize_date(n["birth_date"]) != birth or normalize_name(n["full_name"]) != normalize_name(nm):
            issue(r, "Crosswalk GSIS ID conflicts with name or DOB", pid)
            continue
        linked_ids.add(pid)
        linked.append({"pool":r["pool"], "index":r["index"], "gsis_id":pid,
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
            dest = "free_agent"
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
                         "position":r["position"], "birth_date":birth, "gsis_id":pid,
                         "from_team":current, "to_team":dest, "action":action,
                         "nfl_status":source_status, "match_method":match_method,
                         "approval":"PENDING"})
    proposed.sort(key=lambda r: (r["action"], r["player"], int(r["index"])))
    needs_review.sort(key=lambda r: (r["reason"], r["player"], int(r["index"])))
    linked.sort(key=lambda r: int(r["index"]))
    return proposed, needs_review, linked


def inventory(roster, source=None):
    rows, fields = load_csv(roster)
    validate_game(rows, fields)
    pools = Counter(r["pool"] for r in rows)
    teams = Counter(r["team"] for r in rows)
    active = [r for r in rows if valid_player(r)]
    return {"roster_path":str(roster), "sha256":digest(roster), "records":len(rows),
            "columns":len(fields), "pools":dict(pools), "teams":dict(sorted(teams.items())),
            "eligible_named_primary_records":len(active), "eligible_with_valid_dob":sum(bool(normalize_date(r["birth_date"])) for r in active),
            "noncurrent_teams":dict(sorted(Counter(r["team"] for r in rows if r["team"] not in MEMBERSHIP_CODES and r["team"] not in {"free_agent", "draft_class", "pool"} and not r["team"].startswith("reserve:")).items())),
            "source_compatibility":"91-column format is exported; import/writeback not proven"}


def fetch(season, out):
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


def plan_paths(roster, nfl, map_path, out):
    game, fields = load_csv(roster)
    validate_game(game, fields)
    source, nfl_fields = load_csv(nfl)
    if not REQUIRED_NFL <= set(nfl_fields):
        raise ValueError(f"NFL source requires {sorted(REQUIRED_NFL)}")
    proposals, reviews, links = make_plan(game, source, read_links(map_path))
    out = Path(out)
    save_csv(out/"proposed_transactions.csv", proposals, CHANGE_FIELDS)
    save_csv(out/"needs_review.csv", reviews, REVIEW_FIELDS)
    save_csv(out/"player_crosswalk.csv", links, LINK_FIELDS)
    metadata = {"generated_at_utc":stamp(),"game_sha256":digest(roster),
                "nfl_sha256":digest(nfl), "game_rows":len(game),"nfl_rows":len(source),
                "matched_players":len(links),"proposals":len(proposals),
                "reviews":len(reviews),"actions":dict(Counter(r['action'] for r in proposals)),
                "caveats":["Proposals require approval", "Do not treat a missing NFL row as a release",
                           "Reserve/practice statuses are not auto moved", "No game save or disc has been modified"]}
    save_json(out/"summary.json", metadata)
    print(f"{len(links)} linked, {len(proposals)} proposed, {len(reviews)} need review")
    print(f"Review {out/'proposed_transactions.csv'}; change approval from PENDING to APPROVED only for desired moves")
    return metadata


def stage(roster, changes, out, summary=None):
    src, fields = load_csv(roster)
    validate_game(src, fields)
    original_hash = digest(roster)
    if summary:
        meta = json.loads(Path(summary).read_text(encoding="utf-8"))
        if meta.get("game_sha256") != original_hash:
            raise ValueError("The roster file changed after planning; re-run plan")
    planned, columns = load_csv(changes)
    if not set(CHANGE_FIELDS) <= set(columns):
        raise ValueError("Transaction CSV missing required fields")
    if Path(roster).resolve() == Path(out).resolve():
        raise ValueError("Refusing to overwrite the source roster")
    if Path(out).exists():
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
        if not valid_player(r) or r["team"] != c["from_team"] or display(r) != c["player"] or normalize_date(r["birth_date"]) != c["birth_date"]:
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
        assert all(before[k] == after[k] for k in fields if k != "team")
    validate_game(updated, fields)
    save_csv(out, updated, fields)
    emitted, emitted_fields = load_csv(out)
    if emitted_fields != fields or emitted != updated:
        Path(out).unlink(missing_ok=True)
        raise ValueError("Written CSV did not round-trip exactly")
    report = {"created_at_utc":stamp(),"source_sha256":original_hash,
              "staged_sha256":digest(out),"records":len(updated),"approved_changes":len(approved),
              "changed_columns":["team"],"actions":dict(Counter(c['action'] for c in approved.values())),
              "warning":"This is a staged CSV only. Re-import into Mod Studio and verify in xemu before using."}
    save_json(str(out)+".manifest.json", report)
    print(f"Staged {len(approved)} approved team-only updates at {out}; import pathway remains unverified")
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd",required=True)
    i = sub.add_parser("inspect", help="Summarize an untouched Mod Studio roster export")
    i.add_argument("--roster",type=Path,required=True)
    i.add_argument("--out",type=Path)
    f = sub.add_parser("fetch",help="Fetch season-level NFL roster snapshot")
    f.add_argument("--season",type=int,default=2026)
    f.add_argument("--out",type=Path,default=Path("data/nfl_roster_2026.csv"))
    pl = sub.add_parser("plan",help="Generate transaction proposals from exact identities")
    pl.add_argument("--roster",type=Path,required=True)
    pl.add_argument("--nfl",type=Path,required=True)
    pl.add_argument("--crosswalk",type=Path)
    pl.add_argument("--out",type=Path,default=Path("output"))
    a = sub.add_parser("stage",help="Stage only explicitly APPROVED team changes in a copied CSV")
    a.add_argument("--roster",type=Path,required=True)
    a.add_argument("--changes",type=Path,required=True)
    a.add_argument("--summary",type=Path,help="Optional plan summary to enforce source SHA-256")
    a.add_argument("--out",type=Path,required=True)
    args=p.parse_args()
    if args.cmd == "inspect":
        result=inventory(args.roster)
        if args.out:save_json(args.out,result)
        print(json.dumps(result,indent=2))
    elif args.cmd == "fetch":fetch(args.season,args.out)
    elif args.cmd == "plan":plan_paths(args.roster,args.nfl,args.crosswalk,args.out)
    else:stage(args.roster,args.changes,args.out,args.summary)


if __name__ == "__main__":
    main()
