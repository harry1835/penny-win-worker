"""Working-hours / idle policy for keeping SAP2000 and AutoCAD open."""
from __future__ import annotations

import os
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_sap
from engines import app_keeper, autocad_engine, sap2000_engine

MON_10AM = datetime(2026, 11, 9, 10, 0)   # Monday
MON_6PM = datetime(2026, 11, 9, 18, 0)
SAT_10AM = datetime(2026, 11, 7, 10, 0)


class TestPolicy(unittest.TestCase):
    def test_default_work_hours(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WORK_HOURS", None)
            os.environ.pop("WORK_DAYS", None)
            self.assertTrue(app_keeper.in_work_hours(MON_10AM))
            self.assertFalse(app_keeper.in_work_hours(MON_6PM))
            self.assertFalse(app_keeper.in_work_hours(SAT_10AM))
            self.assertIsNone(app_keeper.sap2000_idle_limit_s(MON_10AM))
            self.assertEqual(app_keeper.sap2000_idle_limit_s(MON_6PM), 600.0)

    def test_custom_hours_and_days(self):
        with patch.dict(os.environ, {"WORK_HOURS": "08:30-18:30", "WORK_DAYS": "Mon-Sat"}):
            self.assertTrue(app_keeper.in_work_hours(MON_6PM))
            self.assertTrue(app_keeper.in_work_hours(SAT_10AM))
        self.assertEqual(app_keeper.parse_days("Sat-Mon"), {5, 6, 0})


class TestSap2000Reaper(unittest.TestCase):
    def setUp(self):
        self.sap = fake_sap.make_sap_module()
        self.exits = []
        obj = self.sap.cHelper(None).GetObject("x")
        obj.ApplicationExit = lambda save: self.exits.append(save)
        sap2000_engine._SAP_STATE.owned = True
        sap2000_engine._SAP_STATE.last_used = 0.0

    def tearDown(self):
        sap2000_engine._SAP_STATE.owned = False

    def _reap(self, when, now_s):
        with patch.object(sap2000_engine, "_load_sap_module", return_value=self.sap), \
             patch.object(app_keeper, "datetime") as dt:
            dt.now.return_value = when
            return sap2000_engine.reap_idle_sap2000(now=now_s)

    def test_kept_open_in_working_hours(self):
        self.assertFalse(self._reap(MON_10AM, now_s=99_999))
        self.assertEqual(self.exits, [])

    def test_closed_after_idle_off_hours(self):
        self.assertFalse(self._reap(MON_6PM, now_s=300))    # 5 min idle: keep
        self.assertTrue(self._reap(MON_6PM, now_s=700))     # 11+ min idle: close
        self.assertEqual(self.exits, [False])
        self.assertFalse(sap2000_engine._SAP_STATE.owned)

    def test_someone_elses_sap2000_is_never_closed(self):
        sap2000_engine._SAP_STATE.owned = False
        self.assertFalse(self._reap(MON_6PM, now_s=99_999))
        self.assertEqual(self.exits, [])


class TestAutoCADReaper(unittest.TestCase):
    def _reap(self, docs_open, now_s):
        quits = []
        acad = types.SimpleNamespace(Documents=types.SimpleNamespace(Count=docs_open), Quit=lambda: quits.append(1))
        client = types.ModuleType("win32com.client")
        client.GetActiveObject = lambda prog: acad
        win32 = types.ModuleType("win32com")
        win32.client = client
        pycom = types.ModuleType("pythoncom")
        pycom.CoInitialize = pycom.CoUninitialize = lambda: None
        autocad_engine._ACAD_STATE.owned = True
        autocad_engine._ACAD_STATE.last_used = 0.0
        with patch.dict(sys.modules, {"win32com": win32, "win32com.client": client, "pythoncom": pycom}):
            done = autocad_engine.reap_idle_autocad(now=now_s)
        return done, quits

    def tearDown(self):
        autocad_engine._ACAD_STATE.owned = False

    def test_closed_after_five_idle_minutes(self):
        self.assertEqual(self._reap(0, now_s=120), (False, []))
        self.assertEqual(self._reap(0, now_s=301), (True, [1]))

    def test_not_closed_while_a_drawing_is_open(self):
        self.assertEqual(self._reap(1, now_s=9_999), (False, []))


if __name__ == "__main__":
    unittest.main()
