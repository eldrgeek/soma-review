import copy
import os
import sys
import unittest

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V2_DIR)

import blockmap  # noqa: E402
import mdblocks  # noqa: E402


def parsed(text):
    _title, blocks = mdblocks.parse_markdown(text)
    return blocks


def initial(doc, marks=None):
    return blockmap.reconcile(None, doc.encode(), parsed(doc), marks or [], now="2026-09-01T00:00:00Z")


class BlockMapSafetyTests(unittest.TestCase):
    def assertSafety(self, marks, mapping):
        blocks = mapping["blocks"]
        by_id = {block["id"]: block for block in blocks}
        for mark in marks:
            if mark.get("deleted") or not mark.get("quote"):
                continue
            if mark.get("unresolved"):
                self.assertTrue(mark["quote"])
                continue
            block = by_id[mark["block_id"]]
            text = blockmap.norm(block["text"])
            covered = text if mark.get("to") is None else text[mark["from"]:mark["to"]]
            self.assertEqual(covered, mark["quote"])

    def test_never_silently_moves(self):
        before = "# Scene\n\nAlpha beta gamma.\n\nSecond paragraph.\n"
        mapping, _, _ = initial(before)
        target = mapping["blocks"][1]
        mark = {"id": "m1", "block_id": target["id"], "from": 6, "to": 10,
                "quote": "beta", "heading_path": ["Scene"]}
        edits = [
            "# Scene\n\nA new first paragraph.\n\nAlpha beta gamma.\n\nSecond paragraph.\n",
            "# Scene\n\nAlpha brave beta gamma.\n\nSecond paragraph.\n",
            "# Scene\n\nAlpha gamma.\n\nSecond paragraph.\n",
            "# Renamed\n\nSecond paragraph.\n\nAlpha beta gamma.\n",
            "# Scene\n\nAlpha beta gamma.\n\nAlpha beta gamma.\n",
        ]
        for after in edits:
            new_map, marks, _ = blockmap.reconcile(
                mapping, after.encode(), parsed(after), [mark], now="2026-09-01T00:01:00Z"
            )
            self.assertSafety(marks, new_map)

    def test_round_trip_restores(self):
        v0 = "# A\n\nA sufficiently distinctive paragraph returns after a branch flip.\n"
        v1 = "# A\n\nTemporary replacement text with a different identity.\n"
        map0, _, _ = initial(v0)
        original_id = map0["blocks"][1]["id"]
        mark = {"id": "m", "block_id": original_id, "from": 2, "to": 14,
                "quote": "sufficiently", "heading_path": ["A"]}
        map1, marks1, _ = blockmap.reconcile(map0, v1.encode(), parsed(v1), [mark])
        self.assertTrue(marks1[0]["unresolved"])
        map2, marks2, _ = blockmap.reconcile(map1, v0.encode(), parsed(v0), marks1)
        self.assertEqual(original_id, map2["blocks"][1]["id"])
        self.assertFalse(marks2[0]["unresolved"])
        self.assertSafety(marks2, map2)

    def test_duplicate_text_never_guesses(self):
        before = "# A\n\nIdentical long paragraph used more than once.\n\nIdentical long paragraph used more than once.\n"
        after = "# A\n\nIdentical long paragraph used more than once.\n"
        mapping, _, _ = initial(before)
        second = mapping["blocks"][2]
        mark = {"id": "m", "block_id": second["id"], "from": 0, "to": None,
                "quote": second["text"], "heading_path": ["A"]}
        new_map, marks, _ = blockmap.reconcile(mapping, after.encode(), parsed(after), [mark])
        self.assertTrue(marks[0]["unresolved"])
        self.assertSafety(marks, new_map)

    def test_self_match_is_identity(self):
        doc = "# A\n\nOne useful paragraph.\n\n---\n\nAnother useful paragraph.\n"
        first, _, _ = initial(doc)
        second, _, report = blockmap.reconcile(first, doc.encode(), parsed(doc), [])
        self.assertEqual([b["id"] for b in first["blocks"]], [b["id"] for b in second["blocks"]])
        self.assertEqual(first["generation"], second["generation"])
        self.assertFalse(report["changed"])

    def test_deterministic_match(self):
        old = parsed("# A\n\nFirst paragraph with enough entropy.\n\nSecond paragraph with enough entropy.\n")
        new = parsed("# A\n\nSecond paragraph with enough entropy.\n\nFirst paragraph with enough entropy.\n")
        a = blockmap.match(old, [], new)
        b = blockmap.match(copy.deepcopy(old), [], copy.deepcopy(new))
        self.assertEqual(a.pairs, b.pairs)
        self.assertEqual(a.ambiguous_new, b.ambiguous_new)

    def test_degenerate_parse_writes_nothing(self):
        doc = "# A\n\nOne.\n\nTwo.\n\nThree.\n"
        mapping, _, _ = initial(doc)
        broken = "# A\n\n```python\nunterminated"
        result, _, report = blockmap.reconcile(mapping, broken.encode(), parsed(broken), [])
        self.assertEqual(mapping, result)
        self.assertEqual("unterminated-fence", report["blocked"])

    def test_quote_beats_in_bounds_offsets(self):
        before = "# A\n\nred blue green\n"
        mapping, _, _ = initial(before)
        block = mapping["blocks"][1]
        mark = {"id": "m", "block_id": block["id"], "from": 4, "to": 8,
                "quote": "blue", "heading_path": ["A"]}
        after = "# A\n\nred teal blue green\n"
        new_map, marks, _ = blockmap.reconcile(mapping, after.encode(), parsed(after), [mark])
        self.assertEqual("blue", marks[0]["quote"])
        self.assertSafety(marks, new_map)


class OffsetTests(unittest.TestCase):
    def mapped_range(self, old, new, start, end):
        edits = blockmap.position_map(old, new)
        return (blockmap.remap_point(start, edits, True),
                blockmap.remap_point(end, edits, False))

    def test_gravity(self):
        old = "0123456789abcdefghijXYZ"
        at_start = old[:10] + "NEW" + old[10:]
        at_end = old[:20] + "NEW" + old[20:]
        self.assertEqual((10, 23), self.mapped_range(old, at_start, 10, 20))
        self.assertEqual((10, 20), self.mapped_range(old, at_end, 10, 20))

    def test_astral_offsets_are_codepoints(self):
        old = "a😀b marked text"
        new = "prefix " + old
        start = len("a😀b ")
        end = start + len("marked")
        mapped = self.mapped_range(old, new, start, end)
        self.assertEqual("marked", new[mapped[0]:mapped[1]])


if __name__ == "__main__":
    unittest.main()
