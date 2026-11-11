import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import update

T = {"recount_pct": 0.25, "very_close_votes": 100, "very_close_pct": 0.5, "watch_votes": 200, "watch_pct": 2.0}

XML = b"""<ElectionResult><Timestamp>11/10/2026 5:06:37 PM PST</Timestamp><ElectionName>Test</ElectionName>
<Contest key="1" text="City Council" voteFor="2" isQuestion="false">
 <VoteType name="Undervotes" votes="180"/><VoteType name="Overvotes" votes="20"/>
 <Choice key="1" text="A" totalVotes="500"/><Choice key="2" text="B" totalVotes="400"/>
 <Choice key="3" text="C" totalVotes="390"/><Choice key="4" text="Write-in" totalVotes="10"/>
</Contest>
<Contest key="2" text="Measure X - Parcel Tax - 2/3rds (66.67%) Vote Required" voteFor="0" isQuestion="false">
 <VoteType name="Undervotes" votes="50"/><VoteType name="Overvotes" votes="0"/>
 <Choice key="5" text="YES" totalVotes="600"/><Choice key="6" text="NO" totalVotes="350"/>
</Contest>
<Contest key="3" text="Governor" voteFor="1" isQuestion="false">
 <Choice key="7" text="X" totalVotes="1"/><Choice key="8" text="Y" totalVotes="2"/>
</Contest></ElectionResult>"""

CFG = {"tiers": T, "include_ballot_measures": True, "exclude_name_patterns": ["^Governor"], "exclude_contests": []}


class Margins(unittest.TestCase):
    def setUp(self):
        meta, contests = update.parse_detail(XML)
        self.r = update.analyze(meta, contests, CFG)
        self.by = {a["name"]: a for a in self.r["contests"]}

    def test_vote_for_n(self):
        a = self.by["City Council"]
        self.assertEqual(a["margin"], 10)              # B 400 - C 390; write-in ignored
        self.assertEqual(a["ballots_cast"], 750)        # (1300 + 180 + 20) / 2
        self.assertAlmostEqual(a["pct"], 100 * 10 / 750)
        self.assertAlmostEqual(a["site_pct"], 100 * 10 / 1300)
        self.assertEqual(a["tier"], 2)

    def test_measure_two_thirds(self):
        a = [x for x in self.r["contests"] if x["type"] == "measure"][0]
        self.assertFalse(a["passing"])                  # 600/950 = 63.2%
        self.assertEqual(a["margin"], 100)              # (633.33 - 600) / (1/3) yes votes needed
        self.assertEqual(a["ballots_cast"], 1000)

    def test_excluded(self):
        self.assertEqual([x["name"] for x in self.r["excluded"]], ["Governor"])

    def test_majority_matches_vote_gap(self):
        meta, cs = update.parse_detail(XML.replace(b"2/3rds (66.67%)", b"Majority"))
        a = [x for x in update.analyze(meta, cs, CFG)["contests"] if x["type"] == "measure"][0]
        self.assertTrue(a["passing"])
        self.assertEqual(a["margin"], 250)              # 600 - 350


if __name__ == "__main__":
    unittest.main()
