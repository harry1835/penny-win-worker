"""
tests/test_worker_api.py — Unit & HTTP Integration Tests for penny-win-worker
=============================================================================
Tests:
  - Process watchdog timeout & recovery (`engines/process_guard.py`)
  - SAP2000 OAPI analysis & check logic (`engines/sap2000_engine.py`)
  - AutoCAD COM & ODA/ezdxf DWG title block editing (`engines/autocad_engine.py`)
  - Unified HTTP Server endpoints (`/health`, `/analyze`, `/sap2000/build-portal`,
    `/autocad/update-title-block`) and Bearer authentication (`worker_server.py`)
"""
from __future__ import annotations

import base64
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib import error as urlerror
from urllib import request as urlrequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker_server
from engines import autocad_engine, process_guard, sap2000_engine


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_json(
    url: str,
    method: str = "GET",
    payload: dict | None = None,
    token: str = "",
) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class TestProcessGuard(unittest.TestCase):
    def test_watchdog_returns_normal_result(self):
        res = process_guard.run_with_watchdog(lambda x, y: x + y, 4, 5, timeout_seconds=5)
        self.assertEqual(res, 9)

    def test_watchdog_times_out_and_triggers_cleanup(self):
        def slow_fn():
            time.sleep(2.0)
            return "done"

        with patch.object(process_guard, "kill_windows_processes", return_value=["SAP2000.exe"]) as mock_kill:
            with self.assertRaises(TimeoutError) as ctx:
                process_guard.run_with_watchdog(
                    slow_fn,
                    timeout_seconds=1,
                    kill_on_timeout=["SAP2000.exe"],
                )
            self.assertIn("SAP2000.exe", str(ctx.exception))
            mock_kill.assert_called_once_with(["SAP2000.exe"])


class TestAutoCADEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dwg = self.tmp / "ECM Plan_CHANGI ROAD.dwg"
        self.dwg.write_bytes(b"AC1032_ORIGINAL_TEMPLATE")
        self.fields = {
            "project_title": "PROPOSED NEW ERECTION AT CHANGI ROAD",
            "road_name": "CHANGI ROAD",
            "ref_no": "A2550-02601-2026",
            "client_name": "PATRICK YEO",
            "contractor_name": "NCK ENTERPRISE PTE LTD",
            "contractor_email": "INFO@NCK.COM.SG",
        }
        self.field_map = {
            "fields": {
                "project_title": {"dwg_attribs": ["PROJECT_TITLE"]},
                "ref_no": {"dwg_attribs": ["REF_NO"]},
            }
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mtext_and_text_transform_rules(self):
        # Waytech PE block is protected
        changed, out = autocad_engine._transform_mtext_string(
            "WAYTECH ENGINEERING CONSULTANTS", 250.0, self.fields
        )
        self.assertFalse(changed)

        # Contractor block is updated with lowercase email
        changed, out = autocad_engine._transform_mtext_string(
            "YAN CONSTRUCTION PTE LTD", 250.0, self.fields
        )
        self.assertTrue(changed)
        self.assertIn("NCK ENTERPRISE PTE LTD", out)
        self.assertIn("info@nck.com.sg", out)

        # Single-line ref_no text is updated
        changed, out = autocad_engine._transform_text_string("A2085-01156-2026", self.fields)
        self.assertTrue(changed)
        self.assertEqual(out, "A2550-02601-2026")

    def test_oda_round_trip_success(self):
        def to_dxf(path, out_dir, target_version="ACAD2018"):
            out = Path(out_dir) / f"{Path(path).stem}.dxf"
            out.write_text("\n".join(self.fields.values()))
            return out

        def to_dwg(path, out_dir, target_version="ACAD2018"):
            out = Path(out_dir) / f"{Path(path).stem}.dwg"
            out.write_bytes(b"AC1032_UPDATED_DWG_BYTES")
            return out

        with patch.object(autocad_engine, "is_autocad_com_available", return_value=False), \
             patch.object(autocad_engine, "find_oda_executable", return_value="/usr/bin/ODAFileConverter"), \
             patch.object(autocad_engine, "dwg_to_dxf_oda", side_effect=to_dxf), \
             patch.object(autocad_engine, "dxf_to_dwg_oda", side_effect=to_dwg), \
             patch.object(autocad_engine, "edit_dxf_title_block", return_value=True):
            ok, msg, out_path, engine = autocad_engine.update_dwg_title_block(
                self.dwg, self.fields, self.field_map
            )
        self.assertTrue(ok)
        self.assertIn("✅", msg)
        self.assertEqual(engine, "oda_ezdxf")
        self.assertEqual(out_path.read_bytes(), b"AC1032_UPDATED_DWG_BYTES")


class TestUnifiedWorkerHTTPServer(unittest.TestCase):
    def setUp(self):
        self.port = _free_port()
        worker_server.PennyWindowsWorkerHandler.auth_token = "win-secret-token"
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), worker_server.PennyWindowsWorkerHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        worker_server.PennyWindowsWorkerHandler.auth_token = ""

    def test_health_endpoint_reports_sap2000_and_autocad(self):
        status, data = _http_json(f"{self.base_url}/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["service"], "penny-win-worker")
        self.assertIn("sap2000_available", data)
        self.assertIn("autocad_available", data)
        self.assertIn("dwg_engine", data)

    def test_unauthorized_post_is_rejected(self):
        status, data = _http_json(
            f"{self.base_url}/analyze",
            method="POST",
            payload={"filename": "t.sdb", "file_b64": "AA=="},
            token="wrong-token",
        )
        self.assertEqual(status, 401)
        self.assertIn("Unauthorized", data["error"])

    def test_sap2000_analyze_endpoint(self):
        fake_res = {
            "engine": "sap2000",
            "overall_pass": True,
            "overall_status": "PASS ✅",
            "governing_util": 0.65,
            "checks": [],
            "summary": {"max_disp_mm": 8.2},
        }
        with patch.object(sap2000_engine, "analyze_sap2000_model", return_value=fake_res) as mock_run:
            status, data = _http_json(
                f"{self.base_url}/sap2000/analyze",
                method="POST",
                payload={
                    "filename": "Frame.sdb",
                    "file_b64": base64.b64encode(b"SDB_BYTES").decode("ascii"),
                    "inputs": {"project": "WLD"},
                },
                token="win-secret-token",
            )
            self.assertEqual(status, 200)
            self.assertTrue(data["overall_pass"])
            self.assertEqual(data["governing_util"], 0.65)
            mock_run.assert_called_once()

    def test_autocad_update_title_block_endpoint(self):
        def fake_update(dwg_path: Path, fields: dict, field_map: dict):
            dwg_path.write_bytes(b"AC1032_EDITED_BY_COM")
            return True, f"✅ title block updated natively via AutoCAD COM ({dwg_path.name})", dwg_path, "autocad_com"

        with patch.object(autocad_engine, "update_dwg_title_block", side_effect=fake_update):
            status, data = _http_json(
                f"{self.base_url}/autocad/update-title-block",
                method="POST",
                payload={
                    "filename": "ECM Plan_CHANGI ROAD.dwg",
                    "file_b64": base64.b64encode(b"AC1032_ORIG").decode("ascii"),
                    "fields": {"project_title": "TEST TITLE"},
                    "field_map": {},
                },
                token="win-secret-token",
            )
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(data["engine"], "autocad_com")
            self.assertEqual(base64.b64decode(data["file_b64"]), b"AC1032_EDITED_BY_COM")


if __name__ == "__main__":
    unittest.main()
