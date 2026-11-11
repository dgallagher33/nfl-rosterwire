import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio_sync import make_plan, stage, save_csv, plan_paths, load_csv

FIELDS = ["pool", "index", "team", "first", "last", "position", "birth_date", "speed", "depth_rank"]


def game():
    return [
        dict(zip(FIELDS, ["primary", "1", "BUF", "Alice", "Example", "CB", "'2001-01-02", "95", "1"])),
        dict(zip(FIELDS, ["primary", "2", "free_agent", "Bobby", "Example", "QB", "'2000-03-04", "85", "0"])),
        dict(zip(FIELDS, ["primary", "3", "ARI", "T.J.", "Moss", "WR", "'1998-07-01", "80", "2"])),
        dict(zip(FIELDS, ["primary", "4", "reserve:BUF", "Cora", "Reserve", "LB", "'1997-04-01", "79", "0"])),
        dict(zip(FIELDS, ["primary", "5", "draft_class", "", "", "SS", "", "0", "0"])),
    ]


def nfl():
    return [
        {"gsis_id": "G1", "full_name": "Alice Example", "birth_date": "2001-01-02", "team": "BUF", "status": "UFA"},
        {"gsis_id": "G2", "full_name": "Bobby Example", "birth_date": "2000-03-04", "team": "BUF", "status": "ACT"},
        {"gsis_id": "G3", "full_name": "TJ Moss", "birth_date": "1998-07-01", "team": "MIA", "status": "ACT"},
        {"gsis_id": "G4", "full_name": "Cora Reserve", "birth_date": "1997-04-01", "team": "BUF", "status": "RES"},
    ]


class RosterWireTests(unittest.TestCase):
    def test_exact_name_dob_and_proposals(self):
        proposed, reviews, links = make_plan(game(), nfl())
        self.assertEqual(len(links), 4)
        self.assertFalse(reviews)
        self.assertEqual(
            [(p["action"], p["player"], p["to_team"]) for p in proposed],
            [("MOVE", "T.J. Moss", "MIA"), ("RELEASE", "Alice Example", "free_agent"),
             ("SIGN", "Bobby Example", "BUF")],
        )
        self.assertTrue(all(p["approval"] == "PENDING" for p in proposed))

    def test_missing_from_source_is_not_release(self):
        proposed, reviews, _ = make_plan(game(), nfl()[1:])
        self.assertEqual(len(proposed), 2)
        self.assertTrue(any("No unique NFL match" in x["reason"] for x in reviews))

    def test_reserve_team_change_is_manual_review(self):
        source = nfl()
        source[-1]["team"] = "MIA"
        proposed, reviews, _ = make_plan(game(), source)
        self.assertEqual(len(proposed), 3)
        self.assertTrue(any("reserve/practice" in x["reason"] for x in reviews))

    def test_staging_modifies_only_team(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src, feed = d / "game.csv", d / "nfl.csv"
            save_csv(src, game(), FIELDS)
            save_csv(feed, nfl(), ["gsis_id", "full_name", "birth_date", "team", "status"])
            plan_paths(src, feed, None, d / "plan")
            changes, fields = load_csv(d / "plan/proposed_transactions.csv")
            changes[0]["approval"] = "APPROVED"
            save_csv(d / "approved.csv", changes, fields)
            report = stage(src, d / "approved.csv", d / "staged.csv", d / "plan/summary.json")
            before, cols = load_csv(src)
            after, staged_cols = load_csv(d / "staged.csv")
            self.assertEqual(cols, staged_cols)
            self.assertEqual(len(before), len(after))
            self.assertEqual(report["approved_changes"], 1)
            diffs = [(a, b) for a, b in zip(before, after) if a != b]
            self.assertEqual(len(diffs), 1)
            self.assertEqual({k for k in cols if diffs[0][0][k] != diffs[0][1][k]}, {"team"})

    def test_stale_source_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src, feed = d / "game.csv", d / "nfl.csv"
            save_csv(src, game(), FIELDS)
            save_csv(feed, nfl(), ["gsis_id", "full_name", "birth_date", "team", "status"])
            plan_paths(src, feed, None, d / "plan")
            changes, fields = load_csv(d / "plan/proposed_transactions.csv")
            changes[0]["approval"] = "APPROVED"
            save_csv(d / "approved.csv", changes, fields)
            revised = game()
            revised[0]["speed"] = "88"
            save_csv(src, revised, FIELDS)
            with self.assertRaisesRegex(ValueError, "changed after planning"):
                stage(src, d / "approved.csv", d / "staged.csv", d / "plan/summary.json")

    def test_special_teams_and_secondary_ignored(self):
        players = game()
        players.append(dict(zip(FIELDS, ["primary", "99", "BRP", "Darius", "Slayton", "WR", "'1997-01-12", "80", "0"])))
        players.append(dict(zip(FIELDS, ["secondary", "0", "pool", "", "", "QB", "", "0", "0"])))
        proposals, reviews, links = make_plan(players, nfl())
        self.assertEqual(len(proposals), 3)
        self.assertFalse(reviews)
        self.assertEqual(len(links), 4)

    def test_missing_birth_date_is_not_auto_matched(self):
        players = game()
        players[0]["birth_date"] = ""
        proposed, reviews, _ = make_plan(players, nfl())
        self.assertEqual(len(proposed), 2)
        self.assertTrue(any("No valid birth date" in x["reason"] for x in reviews))


if __name__ == "__main__":
    unittest.main()
