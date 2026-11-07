"""AutoCAD COM title-block path against a fake AutoCAD object model."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engines import autocad_engine

FIELDS = {
    "project_title": "Proposed erection of a 5-storey data centre",
    "ref_no": "A2611-00123",
    "client_name": "",
    "contractor_name": "",
}


class _Ent:
    def __init__(self, kind, text, y=200.0):
        self.ObjectName = kind
        self.TextString = text
        self.InsertionPoint = (0.0, y, 0.0)
        self.HasAttributes = False

    def Update(self):
        pass


class _Space:
    def __init__(self, ents):
        self.ents = ents
        self.Count = len(ents)

    def Item(self, i):
        return self.ents[i]


class _Doc:
    def __init__(self, ents):
        self.ModelSpace = _Space(ents)
        self.Layouts = types.SimpleNamespace(Count=1, Item=lambda i: types.SimpleNamespace(Name="Model"))
        self.saved = False
        self.closed_with = None

    def Save(self):
        self.saved = True

    def Close(self, save):
        self.closed_with = save


class _ComError(Exception):
    def __init__(self, hresult, msg):
        super().__init__(hresult, msg)
        self.hresult = hresult


class TestAutoCADCom(unittest.TestCase):
    def _run(self, ents, busy_opens=0):
        doc = _Doc(ents)
        calls = {"open": 0}

        def open_(p):
            calls["open"] += 1
            if calls["open"] <= busy_opens:  # AutoCAD still starting: "call was rejected"
                raise _ComError(-2147418111, "Call was rejected by callee.")
            return doc
        acad = types.SimpleNamespace(Visible=False, Documents=types.SimpleNamespace(Open=open_))
        acad.open_calls = calls
        acad.quit_called = False

        def quit_():
            acad.quit_called = True
        acad.Quit = quit_
        win32 = types.ModuleType("win32com")
        client = types.ModuleType("win32com.client")
        client.GetActiveObject = lambda prog: (_ for _ in ()).throw(OSError("not running"))
        client.Dispatch = lambda prog: acad
        win32.client = client
        pycom = types.ModuleType("pythoncom")
        pycom.CoInitialize = lambda: None
        pycom.CoUninitialize = lambda: None
        with patch.dict(sys.modules, {"win32com": win32, "win32com.client": client, "pythoncom": pycom}):
            ok, msg = autocad_engine.update_dwg_via_autocad_com(Path("C:/x/plan.dwg"), FIELDS, {})
        return ok, msg, doc, acad

    def test_edits_verified_and_saved_and_autocad_left_running(self):
        ok, msg, doc, acad = self._run([
            _Ent("AcDbMText", "PROPOSED ERECTION OF SOMETHING", 300),
            _Ent("AcDbText", "A2311-00029"),
            _Ent("AcDbMText", "WAYTECH ENGINEERING — PROFESSIONAL ENGINEER", 300),
        ])
        self.assertTrue(ok, msg)
        self.assertIn("verified", msg)
        self.assertTrue(doc.saved)
        self.assertIn("PROPOSED ERECTION OF A 5-STOREY", doc.ModelSpace.ents[0].TextString)
        self.assertEqual(doc.ModelSpace.ents[1].TextString, "A2611-00123")
        self.assertIn("WAYTECH", doc.ModelSpace.ents[2].TextString)  # PE block untouched
        self.assertFalse(acad.quit_called)  # kept warm for the next DWG

    def test_incomplete_edit_is_not_saved(self):
        # the template's ref-no text doesn't match any rule → ref_no never written
        ok, msg, doc, _ = self._run([
            _Ent("AcDbMText", "PROPOSED ERECTION OF SOMETHING", 300),
            _Ent("AcDbText", "REF: UNKNOWN-FORMAT"),
        ])
        self.assertFalse(ok)
        self.assertIn("ref_no", msg)
        self.assertFalse(doc.saved)
        self.assertFalse(doc.closed_with)


class TestBusyRetry(TestAutoCADCom):
    def test_busy_autocad_is_retried_not_abandoned(self):
        with patch.object(autocad_engine.time, "sleep", lambda s: None):
            ok, msg, doc, acad = self._run([
                _Ent("AcDbMText", "PROPOSED ERECTION OF SOMETHING", 300),
                _Ent("AcDbText", "A2311-00029"),
            ], busy_opens=3)
        self.assertTrue(ok, msg)
        self.assertEqual(acad.open_calls["open"], 4)

    def test_other_errors_are_not_retried(self):
        calls = []

        def boom():
            calls.append(1)
            raise _ComError(-2147352567, "Exception occurred.")
        with self.assertRaises(_ComError):
            autocad_engine._com_retry(boom)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()


class TestAvailability(unittest.TestCase):
    def test_autocad_not_available_without_pywin32(self):
        with patch.object(autocad_engine.sys, "platform", "win32"), \
             patch.object(autocad_engine, "pywin32_installed", return_value=False):
            self.assertFalse(autocad_engine.is_autocad_com_available())


class TestProgIds(unittest.TestCase):
    def test_order(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("AUTOCAD_PROGID", None)
            got = autocad_engine.order_progids(
                ["AutoCAD.Application.25.1", "AutoCAD.Application.26", "AutoCAD.Application.24"],
                curver=None, generic_has_clsid=False)
            self.assertEqual(got, ["AutoCAD.Application.26", "AutoCAD.Application.25.1", "AutoCAD.Application.24"])
            self.assertEqual(autocad_engine.order_progids([], "AutoCAD.Application.26", True),
                             ["AutoCAD.Application", "AutoCAD.Application.26"])
        with patch.dict("os.environ", {"AUTOCAD_PROGID": "AutoCAD.Application.27"}):
            self.assertEqual(autocad_engine.order_progids(["AutoCAD.Application.26"], None, True)[0],
                             "AutoCAD.Application.27")

    def test_invalid_class_string_moves_to_next_name(self):
        tried = []

        def dispatch(p):
            tried.append(p)
            if p == "AutoCAD.Application":
                raise _ComError(-2147221005, "Invalid class string")
            return "acad"
        client = types.SimpleNamespace(Dispatch=dispatch)
        with patch.object(autocad_engine, "autocad_progids",
                          return_value=["AutoCAD.Application", "AutoCAD.Application.26"]):
            self.assertEqual(autocad_engine._start_autocad(client), "acad")
        self.assertEqual(tried, ["AutoCAD.Application", "AutoCAD.Application.26"])
