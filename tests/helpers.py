from collections import Counter
from roster_common import CANONICAL_TEAMS, stamp
from nfl_sources import SOURCE_FIELDS
from studio_sync import save_csv, save_json, digest


def expanded(game, fields):
    fields = fields + [f"attribute_{i}" for i in range(91 - len(fields))]
    rows = [{k: r.get(k, "unchanged") for k in fields} for r in game]
    for pool, count in (("primary", 4096), ("secondary", 68)):
        used = {r["index"] for r in rows if r["pool"] == pool}
        for index in range(count):
            if str(index) in used:
                continue
            row = dict.fromkeys(fields, "unchanged")
            row.update(pool=pool, index=str(index), team="pool", first="", last="", birth_date="")
            rows.append(row)
    return rows, fields


def snapshot(source):
    now = stamp()
    rows = []
    for index, record in enumerate(source):
        row = dict.fromkeys(SOURCE_FIELDS, "")
        row.update(record)
        row.update(provider="espn", provider_player_id=str(index + 1), espn_id=str(index + 1),
                   roster_status=record["status"], fetched_at=now, source_url="https://example.test/roster")
        rows.append(row)
    for team in CANONICAL_TEAMS:
        for index in range(40):
            row = dict.fromkeys(SOURCE_FIELDS, "")
            row.update(provider="espn", provider_player_id=f"{CANONICAL_TEAMS.index(team)+10}{index:03}",
                       gsis_id=f"FILL-{team}-{index}", full_name=f"Fictional {team} {index}", birth_date="2000-01-01",
                       team=team, status="ACT", roster_status="ACT", fetched_at=now,
                       source_url="https://example.test/roster")
            row["espn_id"] = row["provider_player_id"]
            rows.append(row)
    manifest = {"provider": "espn", "complete": True, "teams": dict(Counter(r["team"] for r in rows)),
                "rows": len(rows), "fetched_at": now}
    return rows, manifest


def write_snapshot(path, source):
    rows, manifest = snapshot(source)
    save_csv(path, rows, SOURCE_FIELDS)
    manifest["sha256"] = digest(path)
    save_json(str(path) + ".meta.json", manifest)


def roster_payload(team="BUF"):
    groups = []
    for group, count in (("offense", 20), ("defense", 20), ("specialTeam", 3),
                         ("injuredReserveOrOut", 1), ("practiceSquad", 1)):
        items = []
        for index in range(count):
            pid = len(groups) * 100 + index + 1
            items.append({"id": str(pid), "fullName": f"Fictional Player {pid}",
                          "dateOfBirth": "2000-01-01T00:00Z", "position": {"abbreviation": "QB"},
                          "status": {"type": "active"}})
        groups.append({"position": group, "items": items})
    return {"team": {"abbreviation": team}, "athletes": groups}
