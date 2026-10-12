import itertools
import random
import unittest

from app.services import priority_planner as p


def entry(name, value):
    return {"path": "music/" + name, "media_id": name, "preference": value}


class PriorityPlannerTests(unittest.TestCase):
    def test_equal_priority_insertion_handles_canonical_inversion(self):
        items = p.ordered([entry("a", 100), entry("b", 100), entry("c", 101)], "media")
        result = p.move(items, "music/c", "music/b", "music/a", "media")
        self.assertEqual([v["path"] for v in p.ordered(result, "media")], ["music/a", "music/c", "music/b"])
        self.assertTrue(all(-7 <= v["preference"] <= 500_000 for v in result))

    def test_outside_collision_window_keeps_exact_values(self):
        items = p.ordered([entry("a", 500_000), entry("b", 200), entry("c", 100), entry("d", -7)], "media")
        result = p.move(items, "music/c", "music/b", "music/a", "media")
        self.assertEqual(result[0]["preference"], 500_000)
        self.assertEqual(result[-1]["preference"], -7)

    def test_unrepresentable_range_does_not_mutate_input(self):
        items = [entry("c", 0), entry("b", 0), entry("a", 0)]
        before = [dict(v) for v in items]
        with self.assertRaises(p.PriorityRangeError):
            p.plan(items, 1, "media", minimum=0, maximum=1)
        self.assertEqual(items, before)

    def test_plan_matches_exhaustive_feasibility_for_small_ranges(self):
        rng = random.Random(712)
        for kind in ("media", "directory"):
            for _ in range(160):
                current = p.ordered([entry(chr(65 + i), rng.randrange(3)) for i in range(4)], kind)
                old_at, at = rng.randrange(4), rng.randrange(4)
                moved = current.pop(old_at);current.insert(at, moved)
                expected = [v["path"] for v in current]
                feasible = any([v["path"] for v in p.ordered([
                    dict(item, preference=value) for item, value in zip(current, values)], kind)] == expected
                    for values in itertools.product(range(3), repeat=4))
                try:
                    result = p.plan(current, at, kind, minimum=0, maximum=2)
                except p.PriorityRangeError:
                    self.assertFalse(feasible)
                else:
                    self.assertTrue(feasible)
                    self.assertEqual([v["path"] for v in p.ordered(result, kind)], expected)

    def test_revision_covers_hidden_and_off_page_peers_and_kind(self):
        items = [entry("a", 99), entry("b", 99)]
        self.assertEqual(p.revision("music", "directory", items), p.revision("music", "directory", items[::-1]))
        self.assertNotEqual(p.revision("music", "media", items), p.revision("music", "directory", items))
        self.assertNotEqual(p.revision("music", "media", items), p.revision("music", "media", [*items, entry("hidden", 100)]))

    def test_stale_or_self_move_is_rejected(self):
        for before, after in [("music/a", ""), ("unknown", "music/b"), ("", "")]:
            with self.assertRaises(p.PriorityMoveError):
                p.move([entry("a", 1), entry("b", 0)], "music/a", before, after, "media")
