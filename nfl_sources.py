"""Public live membership adapter. No stale-cache fallback or inferred releases."""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from roster_common import CANONICAL_TEAMS, nfl_team, normalize_date, normalize_name, require_fresh, stamp

BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
IDENTITY_URL = "https://github.com/nflverse/nflverse-data/releases/tag/players"
SOURCE_FIELDS = ["provider", "provider_player_id", "espn_id", "gsis_id", "full_name",
                 "birth_date", "position", "team", "roster_status", "status", "fetched_at",
                 "source_url", "identity_state", "identity_reason", "identity_source"]


class RosterProvider(Protocol):
    def fetch(self) -> tuple[list[dict], dict]: ...


class JsonClient:
    def __init__(self, cache_dir=None, ttl=900, timeout=20, attempts=3, opener=urlopen, sleep=time.sleep):
        if not 0 <= ttl <= 3600 or attempts < 1 or timeout <= 0:
            raise ValueError("Invalid HTTP/cache settings")
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ttl, self.timeout, self.attempts = ttl, timeout, attempts
        self.opener, self.sleep = opener, sleep

    def get(self, url):
        path = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json") if self.cache_dir else None
        if path and path.exists() and self.ttl:
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                require_fresh(cached["fetched_at"], max_age=self.ttl)
                if cached["url"] == url and isinstance(cached["data"], dict):
                    return cached["data"], {"url": url, "fetched_at": cached["fetched_at"], "cached": True}
            except (ValueError, KeyError, OSError):
                pass
        for attempt in range(self.attempts):
            try:
                with self.opener(Request(url, headers={"Accept": "application/json"}), timeout=self.timeout) as response:
                    if response.status != 200:
                        raise ValueError(f"HTTP {response.status}: {url}")
                    data = json.load(response)
                if not isinstance(data, dict):
                    raise ValueError(f"Expected JSON object: {url}")
                fetched = stamp()
                if path:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"url": url, "fetched_at": fetched, "data": data}), encoding="utf-8")
                return data, {"url": url, "fetched_at": fetched, "cached": False}
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                if isinstance(exc, HTTPError) and exc.code not in {429, 500, 502, 503, 504}:
                    raise RuntimeError(f"HTTP {exc.code}: {url}") from exc
                if attempt + 1 == self.attempts:
                    raise RuntimeError(f"Source fetch failed: {url}: {exc}") from exc
                self.sleep(2 ** attempt)


def parse_roster(data, team, fetched_at, url):
    if nfl_team(data.get("team", {}).get("abbreviation")) != team:
        raise ValueError(f"Roster team mismatch: {team}")
    groups = data.get("athletes")
    if not isinstance(groups, list):
        raise ValueError(f"Missing athletes groups: {team}")
    seen_groups, rows = set(), []
    for group in groups:
        label = group.get("position")
        if label in seen_groups or not isinstance(group.get("items"), list):
            raise ValueError(f"Malformed/duplicate athlete group: {team}")
        seen_groups.add(label)
        for athlete in group["items"]:
            pid = str(athlete.get("id") or "")
            name = athlete.get("fullName") or ""
            if not pid.isdigit() or not isinstance(name, str) or not name.strip():
                raise ValueError(f"Missing athlete identity: {team}")
            raw_status = athlete.get("status", {}).get("type", "").lower()
            status = "UNKNOWN"
            if label == "practiceSquad":
                status = "DEV"
            elif label in {"injuredReserveOrOut", "suspended"}:
                status = "RES"
            elif label in {"offense", "defense", "specialTeam"} and raw_status == "active":
                status = "ACT"
            # Reserve/injury evidence overrides a nominal Active status.
            injury_text = json.dumps(athlete.get("injuries", [])).lower()
            if any(word in injury_text for word in ("reserve", "suspend", '"out"', "pup", "nfi")):
                status = "RES"
            rows.append(dict(zip(SOURCE_FIELDS, ["espn", pid, pid, "", name,
                normalize_date(str(athlete.get("dateOfBirth") or "")[:10]),
                athlete.get("position", {}).get("abbreviation", ""), team, status, status,
                fetched_at, url, "unmapped", "No nflverse identity match", ""])))
    if not {"offense", "defense", "specialTeam"} <= seen_groups or len(rows) < 40:
        raise ValueError(f"Empty or incomplete team feed: {team} ({len(rows)} players)")
    if any(not group["items"] for group in groups if group["position"] in {"offense", "defense", "specialTeam"}):
        raise ValueError(f"Empty required position group: {team}")
    return rows


class EspnProvider:
    def __init__(self, client=None):
        self.client = client or JsonClient()

    def fetch(self):
        data, receipt = self.client.get(BASE + "/teams")
        try:
            teams = [entry["team"] for entry in data["sports"][0]["leagues"][0]["teams"]]
            codes = [nfl_team(t["abbreviation"]) for t in teams]
            ids = [str(t["id"]) for t in teams]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("ESPN team-list schema changed") from exc
        if len(codes) != 32 or set(codes) != set(CANONICAL_TEAMS) or len(set(ids)) != 32:
            raise ValueError("ESPN must return exactly 32 unique NFL teams")
        rows, receipts, counts = [], [receipt], {}
        for code, tid in sorted(zip(codes, ids)):
            url = f"{BASE}/teams/{tid}/roster"
            data, receipt = self.client.get(url)
            parsed = parse_roster(data, code, receipt["fetched_at"], url)
            counts[code] = len(parsed)
            rows.extend(parsed)
            receipts.append(receipt)
        ids = [r["espn_id"] for r in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate ESPN athlete across feeds; retry after upstream settles")
        manifest = {"provider": "espn", "complete": True, "teams": counts, "rows": len(rows),
                    "fetched_at": min(r["fetched_at"] for r in receipts), "requests": receipts,
                    "freshness_note": "HTTP retrieval time; ESPN does not guarantee transaction completeness"}
        return sorted(rows, key=lambda r: (r["team"], r["espn_id"])), manifest


def load_identities():
    try:
        import nflreadpy
    except ImportError as exc:
        raise RuntimeError("Install identities dependency: py -m pip install -r requirements.txt") from exc
    frame = nflreadpy.load_players()
    required = {"gsis_id", "espn_id", "display_name", "birth_date", "latest_team", "position"}
    if not required <= set(frame.columns):
        raise ValueError(f"nflreadpy.load_players schema changed: missing {required - set(frame.columns)}")
    return [{k: str(v) if v is not None else "" for k, v in row.items()}
            for row in frame.select(sorted(required)).to_dicts()]


def attach_identities(rows, identities):
    by_espn, by_person = defaultdict(list), defaultdict(list)
    gsis_counts = Counter(r["gsis_id"] for r in identities if r["gsis_id"])
    for identity in identities:
        if identity["espn_id"]:
            by_espn[identity["espn_id"]].append(identity)
        key = (normalize_name(identity["display_name"]), normalize_date(identity["birth_date"]))
        by_person[key].append(identity)
    for row in rows:
        key = (normalize_name(row["full_name"]), row["birth_date"])
        candidates = by_espn.get(row["espn_id"]) or by_person.get(key, [])
        if not candidates:
            continue
        candidate = candidates[0]
        compatible = (len(candidates) == 1 and key[1] and
                      normalize_name(candidate["display_name"]) == key[0] and
                      normalize_date(candidate["birth_date"]) == key[1] and
                      candidate["espn_id"] in {"", row["espn_id"]} and
                      bool(candidate["gsis_id"]) and gsis_counts[candidate["gsis_id"]] == 1)
        row["identity_source"] = IDENTITY_URL
        if compatible:
            row.update(gsis_id=candidate["gsis_id"], identity_state="verified",
                       identity_reason="Unique compatible IDs, normalized full name and exact birthdate")
        else:
            row.update(identity_state="conflict", identity_reason="Ambiguous or conflicting nflverse identity")
    return rows


def validate_snapshot(rows, manifest):
    if manifest.get("provider") != "espn" or manifest.get("complete") is not True:
        raise ValueError("Only complete ESPN snapshots can authorize staging")
    require_fresh(manifest.get("fetched_at"))
    counts = dict(Counter(r["team"] for r in rows))
    if set(counts) != set(CANONICAL_TEAMS) or counts != manifest.get("teams") or any(n < 40 for n in counts.values()):
        raise ValueError("Incomplete or inconsistent snapshot teams")
    if len(rows) != manifest.get("rows"):
        raise ValueError("Snapshot row count mismatch")
    ids = [r.get("espn_id") for r in rows]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("Missing or duplicate ESPN IDs")
    for row in rows:
        require_fresh(row.get("fetched_at"))
        if (row.get("provider") != "espn" or row.get("provider_player_id") != row["espn_id"]
                or row.get("status") != row.get("roster_status") or not row.get("source_url")):
            raise ValueError("Inconsistent canonical source record")
