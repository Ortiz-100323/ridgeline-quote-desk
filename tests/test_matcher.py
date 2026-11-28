"""Matcher tests, built from the RFQ lines in rfqs/ (expected parts chosen from the catalog,
never from answer_key/).  Run:  python -m unittest discover -s tests -v
"""
import unittest

from app.worker.matcher import Matcher, normalize_unit, tokenize


class TokenizeTest(unittest.TestCase):
    def test_specs_survive(self):
        self.assertEqual(tokenize("Hex cap screw 3/8-16 x 1-1/2 in, grade 5"),
                         ["hex", "cap", "screw", "3/8", "16", "1-1/2", "grade5"])

    def test_customer_spellings_match_catalog_spellings(self):
        pairs = [
            ("bolts 3/8 x 1 1/2 gr5", "screw 3/8 x 1-1/2 grade 5"),
            ('2" flange gasket 150#', "flange gasket 2 in 150 lb"),
            ("AW46 hyd oil 5 gal", "hydraulic oil AW 46 5 gallon"),
            ("5HP", "5 HP"),
            ("gloves L", "gloves size L"),
            ("1\" ball valve 316 SS", "ball valve 1 in 316 stainless"),
        ]
        for customer, catalog in pairs:
            with self.subTest(customer=customer):
                self.assertTrue(set(tokenize(customer)) <= set(tokenize(catalog)),
                                f"{tokenize(customer)} vs {tokenize(catalog)}")

    def test_decimal_sizes_are_one_token(self):
        self.assertIn("1.5hp", tokenize("AC motor 1.5 HP"))
        self.assertNotIn("5hp", tokenize("AC motor 1.5 HP"))

    def test_units(self):
        self.assertEqual(normalize_unit("boxes"), "BX")
        self.assertEqual(normalize_unit("EA"), "EA")
        self.assertIsNone(normalize_unit("bucket"))


class MatcherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = Matcher()

    def check(self, as_written, part, method, review, flags=(), **kw):
        r = self.m.match(as_written, **kw)
        self.assertEqual(r.ridgeline_part_number, part, r.note)
        self.assertEqual(r.match_method, method)
        self.assertEqual(r.needs_review, review, f"{r.confidence} {r.flags}")
        for f in flags:
            self.assertIn(f, r.flags)
        if r.confidence is not None:
            self.assertTrue(0 <= r.confidence <= 1)
        return r

    # 1. our own part number
    def test_exact_part_number(self):
        r = self.check("RL159429 x 3", "RL159429", "ridgeline_part_number", False)
        self.assertEqual(r.confidence, 0.99)

    def test_exact_discontinued_is_replaced(self):
        self.check("RL150432", "RL120374", "ridgeline_part_number", False, ["discontinued_replaced"])

    # 2. cross-reference
    def test_cross_reference(self):
        r = self.check("NF-220420 1\" brass ball valve FP", "RL179507", "cross_reference", False,
                       part_number="NF-220420", description='1" brass ball valve FP', unit="EA")
        self.assertEqual(r.confidence, 0.95)

    def test_cross_reference_is_case_and_dash_insensitive(self):
        self.check("nf 220420", "RL179507", "cross_reference", False, part_number="nf 220420")

    def test_cross_reference_unverified(self):
        self.check("APX-VB-B55", "RL125203", "cross_reference", False, ["unverified_cross_reference"],
                   part_number="APX-VB-B55")

    def test_cross_reference_note_needs_review(self):
        self.check("NF-118204", "RL124050", "cross_reference", True, ["cross_reference_note"],
                   part_number="NF-118204")

    def test_cross_reference_to_discontinued(self):
        self.check("APX-HF-JIC08-90", "RL120374", "cross_reference", False, ["discontinued_replaced"],
                   part_number="APX-HF-JIC08-90")

    def test_cross_reference_with_two_targets_is_ambiguous(self):
        r = self.check("NF-610338", "RL177128", "cross_reference", True, ["ambiguous_cross_reference"])
        self.assertEqual({c.part_number for c in r.candidates}, {"RL177128", "RL107716"})

    def test_cross_reference_conflicting_description(self):
        self.check("NF-220420 2 in stainless ball valve", "RL179507", "cross_reference", True,
                   ["cross_reference_conflict"], part_number="NF-220420",
                   description="2 in stainless ball valve")

    def test_unknown_competitor_falls_back_to_description(self):
        self.check("APX-FL-HE10 Hyd. return element 10 micron cellulose", "RL194646", "description", False,
                   ["cross_reference_missing"], part_number="APX-FL-HE10",
                   description="Hyd. return element 10 micron cellulose")

    def test_unknown_competitor_without_description(self):
        self.check("NF-778812 x 4 (whatever you have thats equivalent)", None, None, True,
                   ["cross_reference_missing", "no_match"])

    # 3. description
    def test_description_matches(self):
        cases = [
            ("20 ea 6204-2RS bearings", "RL171482", "EA"),
            ("10 A42 v-belts", "RL112676", "EA"),
            ('1/2-13 x 2" grade 8 hex bolts, 2 boxes', "RL165435", "BX"),
            ("5 HP TEFC motor, 1800 rpm, 3 phase - qty 2", "RL110322", "EA"),
            ('2" flange gaskets 150# - qty 40', "RL118373", "EA"),
            ("food grade grease cartridges - 3 cases", "RL184696", "CS"),
            ("6205 bearings sealed qty 24", "RL104165", "EA"),
            ("o-rings 214 buna", "RL165909", None),
            ("6204 2RS beraings", "RL171482", None),          # typo
            ('1/2" brass ball valve FP', "RL172063", "EA"),
        ]
        for text, part, unit in cases:
            with self.subTest(text=text):
                self.check(text, part, "description", False, unit=unit)

    def test_size_decides(self):
        r = self.m.match("6205 bearings sealed")
        self.assertNotIn(r.ridgeline_part_number, {"RL183810", "RL171482"})   # 6201, 6204

    def test_vague_line_is_ambiguous(self):
        self.check("Same grease cartridges as last order, 2 cases", "RL195561", "description", True,
                   ["ambiguous_match"], unit="CS")

    def test_unit_mismatch_needs_review(self):
        self.check("bolts 3/8 x 1 1/2 gr5 - 300 (we usually get boxes)", "RL142504", "description", True,
                   ["unit_mismatch"], unit="EA")

    def test_unit_breaks_the_tie(self):
        self.check("nitrile gloves L - a few boxes", "RL153354", "description", False, unit="BX")

    def test_not_in_catalog(self):
        self.check("forklift tires 18x7-8 x4", None, None, True, ["no_match"])
        self.check("Chlorine residual test kit", None, None, True, ["no_match"])

    def test_from_image_always_needs_review(self):
        self.check("RL159429 x 3", "RL159429", "ridgeline_part_number", True, ["from_image"], from_image=True)

    def test_match_line_from_extractor(self):
        from app.worker.schemas import ExtractedLine
        base = dict(line=1, quantity=None, competitor=None, customer_note=None)
        sized = ExtractedLine(**base, as_written="6204-2RS bearings", unit="EA", part_number="6204-2RS",
                              part_number_kind="manufacturer", description="Ball bearing 6204-2RS sealed",
                              issues=[], extraction_confidence=0.97)
        r = self.m.match_line(sized)
        self.assertEqual((r.ridgeline_part_number, r.match_method, r.needs_review),
                         ("RL171482", "description", False))
        vague = ExtractedLine(**base, as_written="nitrile gloves L - a few boxes", unit="box",
                              part_number=None, part_number_kind=None, description="Nitrile gloves size L",
                              issues=["quantity_unclear"], extraction_confidence=0.9)
        r = self.m.match_line(vague)
        self.assertEqual(r.ridgeline_part_number, "RL153354")
        self.assertTrue(r.needs_review)
        self.assertIn("quantity_unclear", r.flags)
        misread = ExtractedLine(**base, as_written="RL159429", unit=None, part_number="RL159429",
                                part_number_kind="ridgeline", description=None, issues=[],
                                extraction_confidence=0.5)
        r = self.m.match_line(misread, from_image=True)
        self.assertEqual(r.confidence, 0.5)
        self.assertIn("low_extraction_confidence", r.flags)
        self.assertIn("from_image", r.flags)

    def test_line_fields_follow_the_contract(self):
        fields = self.m.match("RL159429").to_line_fields()
        self.assertEqual(set(fields), {"ridgeline_part_number", "match_method", "confidence",
                                       "needs_review", "flags", "note"})


class DatabaseCatalogTest(unittest.TestCase):
    def test_db_catalog_equals_csv_catalog(self):
        try:
            from app.db import connect
            conn = connect(connect_timeout=3)
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"database unavailable: {exc}")
        from app.catalog import load_catalog, load_catalog_from_db
        with conn:
            db = load_catalog_from_db(conn)
        csv = load_catalog()
        self.assertEqual(set(db.products), set(csv.products))
        seed_targets = {k: {x.ridgeline_part_number for x in v} for k, v in csv.cross_references.items()}
        for key, targets in seed_targets.items():
            self.assertTrue(targets <= {x.ridgeline_part_number for x in db.cross_references[key]}, key)
        self.assertEqual(Matcher(db).match("NF-220420").ridgeline_part_number, "RL179507")


if __name__ == "__main__":
    unittest.main()
