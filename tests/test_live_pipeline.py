import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import URLError

from helpers import expanded, roster_payload, snapshot, write_snapshot
from test_studio_sync import FIELDS, game, nfl
from nfl_sources import EspnProvider, JsonClient, attach_identities, parse_roster, validate_snapshot
from roster_common import CANONICAL_TEAMS, nfl_team, normalize_date, normalize_name, stamp
from studio_sync import (load_csv, make_plan, plan_paths, save_csv, save_json, stage,
                         validate_expanded, read_links, LINK_FIELDS)


class ProviderTests(unittest.TestCase):
    def test_groups_override_active_status(self):
        rows = parse_roster(roster_payload(), "BUF", stamp(), "url")
        self.assertEqual(len(rows), 45)
        self.assertEqual(rows[-2]["status"], "RES")
        self.assertEqual(rows[-1]["status"], "DEV")
        self.assertEqual(rows[0]["birth_date"], "2000-01-01")

    def test_invalid_and_incomplete_payloads(self):
        for change in (lambda d: d.update(team={"abbreviation": "MIA"}),
                       lambda d: d.update(athletes=[]),
                       lambda d: d["athletes"][0].update(items=[]),
                       lambda d: d["athletes"][0]["items"][0].update(id="")):
            data = roster_payload()
            change(data)
            with self.assertRaises(ValueError):
                parse_roster(data, "BUF", stamp(), "url")

    def test_unknown_status_is_never_assumed_active(self):
        data = roster_payload()
        data["athletes"][0]["items"][0].pop("status")
        self.assertEqual(parse_roster(data, "BUF", stamp(), "url")[0]["status"], "UNKNOWN")

    def test_all_32_teams_and_duplicate_ids(self):
        teams = [{"team": {"id": str(i), "abbreviation": t}} for i, t in enumerate(CANONICAL_TEAMS)]
        def get(url):
            if url.endswith("/teams"):
                data = {"sports": [{"leagues": [{"teams": teams}]}]}
            else:
                i = int(url.split("/")[-2])
                data = roster_payload(CANONICAL_TEAMS[i])
                for group in data["athletes"]:
                    for a in group["items"]:
                        a["id"] = str(i * 1000 + int(a["id"]))
            return data, {"url": url, "fetched_at": stamp(), "cached": False}
        rows, meta = EspnProvider(Mock(get=get)).fetch()
        self.assertEqual(len(meta["teams"]), 32)
        validate_snapshot(rows, meta)
        def duplicate_get(url):
            data, receipt = get(url)
            if not url.endswith("/teams"):
                data["athletes"][0]["items"][0]["id"] = "9999999"
            return data, receipt
        with self.assertRaisesRegex(ValueError, "Duplicate ESPN"):
            EspnProvider(Mock(get=duplicate_get)).fetch()
        teams.pop()
        with self.assertRaisesRegex(ValueError, "32"):
            EspnProvider(Mock(get=get)).fetch()

    def test_snapshot_freshness_and_integrity(self):
        rows, meta = snapshot(nfl())
        validate_snapshot(rows, meta)
        for mutation in (lambda r, m: m.update(fetched_at="2000-01-01T00:00:00+00:00"),
                         lambda r, m: m.update(complete=False),
                         lambda r, m: r.pop(),
                         lambda r, m: r[0].update(espn_id=r[1]["espn_id"]),
                         lambda r, m: r[0].update(fetched_at="2099-01-01T00:00:00+00:00")):
            r, m = copy.deepcopy(rows), copy.deepcopy(meta)
            mutation(r, m)
            with self.assertRaises(ValueError):
                validate_snapshot(r, m)

    def test_retries_and_no_stale_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            def response():
                result = io.BytesIO(b'{"ok": true}')
                result.status = 200
                return result
            opener = Mock(side_effect=[URLError("down"), response()])
            client = JsonClient(d, opener=opener, sleep=lambda _: None)
            _, receipt = client.get("https://example.test")
            self.assertEqual(opener.call_count, 2)
            self.assertFalse(receipt["cached"])
            self.assertTrue(client.get("https://example.test")[1]["cached"])
            cache = next(Path(d).glob("*.json"))
            payload = json.loads(cache.read_text())
            payload["fetched_at"] = "2000-01-01T00:00:00+00:00"
            cache.write_text(json.dumps(payload))
            client.opener = Mock(side_effect=URLError("offline"))
            with self.assertRaises(RuntimeError):
                client.get("https://example.test")
            self.assertEqual(client.opener.call_count, 3)

    def test_http_and_json_failures(self):
        for body, status in ((b'[]', 200), (b'bad', 200), (b'{}', 503)):
            response = io.BytesIO(body)
            response.status = status
            with self.assertRaises(RuntimeError):
                JsonClient(attempts=1, opener=Mock(return_value=response)).get("https://example.test")


class IdentityTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual([nfl_team(t) for t in ("JAC", "LA", "WSH", "ARZ")], ["JAX", "LAR", "WAS", "ARI"])
        self.assertIsNone(nfl_team("NY"))
        self.assertEqual(normalize_name("T.J. Móss Jr."), normalize_name("TJ Moss"))
        self.assertEqual(normalize_date("2020-02-31"), "")

    def test_cross_source_conflicts(self):
        base = {"gsis_id": "G1", "espn_id": "1", "display_name": "Alice Example", "birth_date": "2001-01-02"}
        row = snapshot(nfl())[0][0]
        self.assertEqual(attach_identities([dict(row)], [base])[0]["identity_state"], "verified")
        for identities in ([base, base], [dict(base, birth_date="")],
                           [dict(base, espn_id="2")], [base, dict(base, espn_id="9")]):
            self.assertEqual(attach_identities([dict(row)], identities)[0]["identity_state"], "conflict")

    def test_duplicate_game_identities_withhold_both(self):
        players = game()
        players.append(dict(players[1], index="20"))
        proposals, reviews, _ = make_plan(players, nfl())
        self.assertFalse(any(p["action"] == "SIGN" for p in proposals))
        self.assertEqual(sum("Duplicate game" in r["reason"] for r in reviews), 2)

    def test_duplicate_name_birthdate_with_distinct_ids(self):
        source = nfl()
        _, _, links = make_plan(game(), source)
        source.append(dict(source[1], gsis_id="OTHER"))
        saved = {(r["pool"], r["index"]): r for r in links}
        for crosswalk in (None, saved):
            proposals, _, _ = make_plan(game(), source, crosswalk)
            self.assertFalse(any(p["action"] == "SIGN" for p in proposals))

    def test_duplicate_source_identifiers_withhold_first(self):
        for field in ("gsis_id", "espn_id"):
            source = nfl()
            source[1][field] = "duplicate"
            source[2][field] = "duplicate"
            proposals, reviews, _ = make_plan(game(), source)
            self.assertFalse(proposals)
            self.assertTrue(any("duplicate NFL" in r["reason"] for r in reviews))

    def test_reused_and_incomplete_crosswalk(self):
        for link in ({"gsis_id": "G2"}, {"gsis_id": "G2", "player": "Someone Else", "birth_date": "2000-03-04"}):
            proposals, _, _ = make_plan(game(), nfl(), {("primary", "2"): link})
            self.assertFalse(any(p["action"] == "SIGN" for p in proposals))

    def test_persistent_link_survives_absence_then_returns(self):
        _, _, links = make_plan(game(), nfl())
        saved = {(r["pool"], r["index"]): r for r in links}
        proposals, _, retained = make_plan(game(), nfl()[2:], saved)
        self.assertEqual(len(retained), len(links))
        self.assertFalse(any(p["action"] == "SIGN" for p in proposals))
        again = {(r["pool"], r["index"]): r for r in retained}
        self.assertEqual(make_plan(game(), nfl(), again)[0], make_plan(game(), nfl())[0])

    def test_duplicate_crosswalk_ids_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "links.csv"
            _, _, links = make_plan(game(), nfl())
            links[1]["gsis_id"] = links[0]["gsis_id"]
            save_csv(path, links, LINK_FIELDS)
            with self.assertRaises(ValueError):
                read_links(path)

    def test_determinism_and_applied_moves(self):
        result = make_plan(game(), nfl())
        self.assertEqual(result, make_plan(game(), list(reversed(nfl()))))
        players = game()
        players[1]["team"] = "BUF"
        players[2]["team"] = "MIA"
        self.assertFalse(make_plan(players, nfl())[0])

    def test_release_claim_requires_evidence_adapter(self):
        source = nfl()
        source[0]["release_verified"] = "YES"
        proposals, reviews, _ = make_plan(game(), source)
        self.assertFalse(any(p["action"] == "RELEASE" for p in proposals))
        self.assertTrue(any("independent transaction evidence" in r["reason"] for r in reviews))

    def test_practice_squad_change_is_review_only(self):
        source = nfl()
        source[1]["status"] = "DEV"
        self.assertFalse(any(p["action"] == "SIGN" for p in make_plan(game(), source)[0]))


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster, self.feed = self.root / "game.csv", self.root / "feed.csv"
        save_csv(self.roster, *expanded(game(), FIELDS))
        write_snapshot(self.feed, nfl())
        self.plan = self.root / "plan"
        plan_paths(self.roster, self.feed, None, self.plan)
        self.changes, self.summary = self.plan / "proposed_transactions.csv", self.plan / "summary.json"
        self.out = self.root / "staged.csv"
        rows, fields = load_csv(self.changes)
        rows[0]["approval"] = "APPROVED"
        save_csv(self.changes, rows, fields)

    def test_full_expanded_preservation(self):
        report = stage(self.roster, self.changes, self.out, self.summary)
        self.assertEqual(report["pools"], {"primary": 4096, "secondary": 68})
        before, fields = load_csv(self.roster)
        after, after_fields = load_csv(self.out)
        self.assertEqual(fields, after_fields)
        self.assertEqual(len(fields), 91)
        changes = []
        for a, b in zip(before, after, strict=True):
            self.assertEqual((a["pool"], a["index"]), (b["pool"], b["index"]))
            for field in fields:
                if a[field] != b[field]:
                    changes.append((a["pool"], field))
        self.assertEqual(changes, [("primary", "team")])

    def test_tampered_approval_content(self):
        rows, fields = load_csv(self.changes)
        for field, value in (("to_team", "DEN"), ("gsis_id", "other"), ("action", "RELEASE")):
            modified = copy.deepcopy(rows)
            modified[0][field] = value
            save_csv(self.changes, modified, fields)
            with self.assertRaisesRegex(ValueError, "Proposal contents"):
                stage(self.roster, self.changes, self.out, self.summary)
            self.assertFalse(self.out.exists())

    def test_invalid_approval_and_repeated_stage(self):
        rows, fields = load_csv(self.changes)
        rows[0]["approval"] = "YES"
        save_csv(self.changes, rows, fields)
        with self.assertRaisesRegex(ValueError, "Invalid approval"):
            stage(self.roster, self.changes, self.out, self.summary)
        rows[0]["approval"] = "APPROVED"
        save_csv(self.changes, rows, fields)
        stage(self.roster, self.changes, self.out, self.summary)
        with self.assertRaisesRegex(ValueError, "already exists"):
            stage(self.roster, self.changes, self.out, self.summary)

    def test_corrupt_serialization_never_publishes(self):
        from unittest.mock import patch
        with patch("studio_sync.save_csv", side_effect=lambda path, *args: Path(path).write_text("wrong\n")):
            with self.assertRaisesRegex(ValueError, "round-trip"):
                stage(self.roster, self.changes, self.out, self.summary)
        self.assertFalse(self.out.exists())

    def test_missing_summary_and_expired_approval(self):
        with self.assertRaisesRegex(ValueError, "summary is required"):
            stage(self.roster, self.changes, self.out)
        meta = json.loads(self.summary.read_text())
        meta["source_manifest"]["fetched_at"] = "2000-01-01T00:00:00+00:00"
        save_json(self.summary, meta)
        with self.assertRaisesRegex(ValueError, "Stale"):
            stage(self.roster, self.changes, self.out, self.summary)

    def test_dry_run_no_output(self):
        self.assertTrue(stage(self.roster, self.changes, self.out, self.summary, dry_run=True)["dry_run"])
        self.assertFalse(self.out.exists())
        out = self.root / "dry-plan"
        plan_paths(self.roster, self.feed, None, out, dry_run=True)
        self.assertFalse(out.exists())

    def test_incompatible_csv_and_short_row(self):
        with self.assertRaisesRegex(ValueError, "4096"):
            validate_expanded(game(), FIELDS)
        bad = self.root / "bad.csv"
        bad.write_text("a,b\n1\n")
        with self.assertRaises(ValueError):
            load_csv(bad)

    def test_modified_source_hash(self):
        self.feed.write_text(self.feed.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            plan_paths(self.roster, self.feed, None, self.root / "other")

    def test_legacy_source_inspection_cannot_stage(self):
        Path(str(self.feed) + ".meta.json").unlink()
        save_csv(self.feed, nfl(), list(nfl()[0]))
        dest = self.root / "legacy"
        plan_paths(self.roster, self.feed, None, dest)
        with self.assertRaisesRegex(ValueError, "Unverified"):
            stage(self.roster, dest / "proposed_transactions.csv", self.out, dest / "summary.json")
