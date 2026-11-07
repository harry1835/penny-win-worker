"""Engine extraction tests against a fake OAPI (no SAP2000 needed)."""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_sap
from engines import sap2000_engine, sap2000_post


class TestPost(unittest.TestCase):
    def test_modal_filtered_by_type_not_name(self):
        self.assertTrue(sap2000_post.is_non_physical_case("EQ-X", 3))
        self.assertTrue(sap2000_post.is_non_physical_case("BUCK1", 10))
        self.assertFalse(sap2000_post.is_non_physical_case("MODAL-ish name", 1))
        self.assertTrue(sap2000_post.is_non_physical_case("MODES", None))

    def test_udl_simply_supported_sag(self):
        L, w, EI = 3.0, 2.0, 1000.0
        sta = [L * k / 20 for k in range(21)]
        m = [w * s * (L - s) / 2 for s in sta]
        v = sap2000_post.bending_deflection(sta, m, EI)
        exact = 5 * w * L ** 4 / (384 * EI)
        self.assertAlmostEqual(abs(v[10]), exact, delta=exact * 0.01)
        self.assertLess(v[10], 0)  # sagging moment → downward along +2

    def test_local_axes(self):
        e1, e2, e3 = sap2000_post.frame_local_axes((0, 0, 0), (1, 0, 0))
        self.assertEqual(e2, (0.0, 0.0, 1.0))
        self.assertAlmostEqual(e3[1], -1.0)
        e1, e2, e3 = sap2000_post.frame_local_axes((0, 0, 0), (0, 0, 3))
        self.assertEqual(e2, (1.0, 0.0, 0.0))

    def test_chains_join_collinear_segments_and_stop_at_supports(self):
        joints = {"a": (0, 0, 0), "b": (1, 0, 0), "c": (2, 0, 0), "d": (1, 1, 0)}
        frames = [
            {"name": "1", "i": "a", "j": "b"},
            {"name": "2", "i": "b", "j": "c"},
            {"name": "3", "i": "b", "j": "d"},
        ]
        chains = sap2000_post.build_member_chains(joints, frames, ["a", "c"])
        by_frames = {tuple(sorted(c["frames"])): c for c in chains}
        self.assertIn(("1", "2"), by_frames)
        self.assertAlmostEqual(by_frames[("1", "2")]["length"], 2.0)
        self.assertTrue(by_frames[("3",)]["cantilever"])
        self.assertEqual(by_frames[("3",)]["joints"][0], "b")  # root first


class TestEngineWithFakeOAPI(unittest.TestCase):
    def setUp(self):
        self.sap = fake_sap.make_sap_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.model_path = Path(self.tmp.name) / "Grating.sdb"
        self.model_path.write_bytes(b"fake")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, **inputs):
        with patch.object(sap2000_engine, "_load_sap_module", return_value=self.sap):
            return sap2000_engine._analyze_sap2000_model_inner(self.model_path, inputs=inputs)

    def test_modal_never_reaches_envelopes(self):
        res = self._run()
        self.assertEqual(res["inputs"]["load_cases"], "DEAD, LIVE")
        self.assertEqual(res["inputs"]["excluded_cases"], "MODAL")
        s = res["summary"]
        self.assertLess(abs(s["max_p_kN"]), 1.0)
        self.assertLess(abs(s["max_m3_kNm"]), 10.0)
        self.assertLess(abs(s["max_f1_kN"]), 1.0)
        self.assertLess(s["max_disp_mm"], 5.0)

    def test_member_deflection_check_uses_span_ratio(self):
        res = self._run(deflection="yes")
        defl = [c for c in res["checks"] if c["name"].startswith("Member Deflection")]
        self.assertEqual(len(defl), 1)
        top = res["member_deflections"][0]
        # bearing bar 1.0 m, LIVE 1 kN/m: 5wL^4/384EI on top of edge-bar chord
        exact_mm = 5 * fake_sap.W_LIVE * 1.0 ** 4 / (384 * fake_sap.EI) * 1000
        bearing = [d for d in res["member_deflections"] if abs(d["span_m"] - 1.0) < 1e-9]
        self.assertTrue(bearing)
        self.assertAlmostEqual(max(d["delta_mm"] for d in bearing), exact_mm, delta=exact_mm * 0.05)
        self.assertEqual(top["ratio"], 200.0)
        self.assertIn("L/200", defl[0]["capacity"])

    def test_explicit_limit_keeps_absolute_nodal_check(self):
        res = self._run(deflection_limit_mm=10)
        names = [c["name"] for c in res["checks"]]
        self.assertTrue(any(n.startswith("Max Nodal Deflection") for n in names))

    def test_views_payload(self):
        res = self._run(deflection="yes")
        v = res["views"]
        self.assertEqual(v["version"], "25.3.1")
        self.assertIn(v["plot_combo"], ("1.35G+1.50Q", "DSTL2"))
        self.assertEqual(v["deflection_case"], "LIVE")
        self.assertEqual(v["load_pattern"], "LIVE")
        self.assertEqual(v["design_code"], "EN 1993-1-1:2005/A1:2014")
        self.assertEqual(len(v["frames"]), len(self.sap.model.frames))
        ff = next(iter(v["frame_forces"].values()))
        self.assertEqual(len(ff["sta"]), fake_sap.N_STA)
        self.assertTrue(v["loads"]["frame_dist"])
        self.assertEqual(v["loads_by_pattern"]["DEAD"]["self_weight"], 1.0)
        self.assertEqual(v["loads_by_pattern"]["LIVE"]["area"][0]["val"], 1.5)
        self.assertEqual(v["areas"][0]["name"] if "areas" in v else None, "A1")
        self.assertIn("V3_max", ff)
        self.assertIn("M2_max", ff)
        self.assertTrue(all(ei[1] > 0 for ei in v["stiffness"].values()))
        self.assertTrue(v["reactions"])
        self.assertTrue(all(abs(r[0]) < 1 for r in v["reactions"].values()))
        self.assertEqual(len(v["stress_ratios"]), len(self.sap.model.frames))


class TestNewBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.model_path = Path(self.tmp.name) / "fLASHING.sdb"
        self.model_path.write_bytes(b"fake")

    def tearDown(self):
        self.tmp.cleanup()
        fake_sap.SPLIT_FRONT_EDGE = True

    def _run(self, **inputs):
        sap = fake_sap.make_sap_module()
        with patch.object(sap2000_engine, "_load_sap_module", return_value=sap):
            return sap2000_engine._analyze_sap2000_model_inner(self.model_path, inputs=inputs), sap

    def test_deflection_is_opt_in(self):
        res, _ = self._run()
        self.assertFalse(any("Deflection" in c["name"] for c in res["checks"]))
        self.assertEqual(res["member_deflections"], [])
        self.assertEqual(res["joint_displacements"], [])
        self.assertNotIn("joint_displ", res["views"])
        self.assertEqual(res["inputs"]["deflection_criteria"], "Not checked")
        res, _ = self._run(deflection="yes")
        self.assertTrue(any("Deflection" in c["name"] for c in res["checks"]))

    def test_auto_design_combos_shown_as_formula(self):
        res, _ = self._run()
        self.assertEqual(res["inputs"]["load_combos"], "1.35G+1.50Q, 1.35G, 1.35G+1.5Q")
        self.assertIn("1.35G+1.5Q", res["checks"][0]["name"])
        self.assertNotIn("DSTL", json.dumps(res["steel_design_results"]))
        self.assertTrue(res["steel_design_results"][0]["combo"].startswith("1.35G+1.5Q (@"))

    def test_bars_meeting_a_beam_midspan_are_connected(self):
        fake_sap.SPLIT_FRONT_EDGE = False
        res, sap = self._run(deflection="yes")
        frames = {f["name"]: f for f in res["views"]["frames"]}
        host = next(f for f in frames.values() if f["section"] == "100X100X6")
        self.assertEqual(len(host["interior"]), fake_sap.NX - 2)
        # no bearing bar is treated as a cantilever any more
        self.assertFalse(any(d["cantilever"] for d in res["member_deflections"]))
        host_defl = next(d for d in res["member_deflections"] if host["name"] in d["frames"])
        self.assertGreater(host_defl["delta_mm"], 0.0)


class TestFormula(unittest.TestCase):
    def test_formula(self):
        f = sap2000_post.combo_formula
        self.assertEqual(f([("DEAD", 1.35), ("LIVE", 1.5)], {"DEAD": "G", "LIVE": "Q"}), "1.35G+1.5Q")
        self.assertEqual(f([("DEAD", 1.0), ("WIND", -1.5)], {"DEAD": "G", "WIND": "W"}), "G-1.5W")
        self.assertEqual(f([("DEAD", 1.35), ("SDL", 1.35)], {"DEAD": "G", "SDL": "G"}), "1.35DEAD+1.35SDL")

    def test_interior_joints(self):
        joints = {"a": (0, 0, 0), "b": (4, 0, 0), "m": (1, 0, 0), "off": (2, 0.1, 0)}
        out = sap2000_post.interior_joints(joints, [{"name": "1", "i": "a", "j": "b"}])
        self.assertEqual(out, {"1": [["m", 0.25]]})


if __name__ == "__main__":
    unittest.main()
