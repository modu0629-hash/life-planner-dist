import unittest
from verify import verify_consensus


def C(date, time=None, title="t", **kw):
    d = {"start_date": date, "start_time": time, "title": title}
    d.update(kw)
    return d


class TestVerifyConsensus(unittest.TestCase):
    def test_unanimous_single_is_certain(self):
        e = C("2026-07-06", "15:00")
        out = verify_consensus([[dict(e)], [dict(e)], [dict(e)]])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["confidence"], "certain")
        self.assertEqual(out[0]["verify_note"], "")
        self.assertEqual(out[0]["start_time"], "15:00")

    def test_time_split_majority_wins_and_ambiguous(self):
        out = verify_consensus([
            [C("2026-07-06", "15:00")],
            [C("2026-07-06", "15:00")],
            [C("2026-07-06", "03:00")],
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["start_time"], "15:00")
        self.assertEqual(out[0]["confidence"], "ambiguous")
        self.assertIn("15:00(2)", out[0]["verify_note"])
        self.assertIn("03:00(1)", out[0]["verify_note"])

    def test_extra_event_in_one_run_is_ambiguous(self):
        out = verify_consensus([
            [C("2026-07-06", "15:00"), C("2026-07-07", "10:00")],
            [C("2026-07-06", "15:00")],
            [C("2026-07-06", "15:00")],
        ])
        by_date = {o["start_date"]: o for o in out}
        self.assertEqual(by_date["2026-07-06"]["confidence"], "certain")
        self.assertEqual(by_date["2026-07-07"]["confidence"], "ambiguous")

    def test_two_distinct_events_same_day_both_certain(self):
        out = verify_consensus([
            [C("2026-07-06", "10:00"), C("2026-07-06", "15:00")],
            [C("2026-07-06", "10:00"), C("2026-07-06", "15:00")],
            [C("2026-07-06", "10:00"), C("2026-07-06", "15:00")],
        ])
        self.assertEqual(len(out), 2)
        self.assertTrue(all(o["confidence"] == "certain" for o in out))

    def test_empty_lists_return_empty(self):
        self.assertEqual(verify_consensus([[], [], []]), [])

    def test_candidate_without_date_ignored(self):
        out = verify_consensus([[{"title": "x", "start_time": "10:00"}], [], []])
        self.assertEqual(out, [])

    def test_single_run_event_note_says_one_run(self):
        # 7/7 일정은 r1에만 1회 → "3벌이 제각각" 아닌 "1벌에서만 발견"
        out = verify_consensus([
            [C("2026-07-06", "15:00"), C("2026-07-07", "10:00")],
            [C("2026-07-06", "15:00")],
            [C("2026-07-06", "15:00")],
        ])
        note = {o["start_date"]: o["verify_note"] for o in out}["2026-07-07"]
        self.assertEqual(note, "1벌에서만 발견")

    def test_unanimous_event_on_contested_day_has_clear_note(self):
        # 같은 날 10:00(3/3) + 15:00(2/3): 10:00은 만장일치지만 날짜가 갈려 ambiguous
        out = verify_consensus([
            [C("2026-07-06", "10:00"), C("2026-07-06", "15:00")],
            [C("2026-07-06", "10:00"), C("2026-07-06", "15:00")],
            [C("2026-07-06", "10:00")],
        ])
        notes = {o["start_time"]: o["verify_note"] for o in out}
        self.assertEqual(notes["10:00"], "같은 날 다른 시간과 갈림")
        self.assertEqual(notes["15:00"], "3벌 중 2벌만 일치")
        self.assertTrue(all(o["confidence"] == "ambiguous" for o in out))


if __name__ == "__main__":
    unittest.main()
