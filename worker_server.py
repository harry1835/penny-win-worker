"""
worker_server.py — Unified Windows CAD/CAE HTTP Worker for Penny COS Bot
========================================================================
Runs on the Windows VM (default port 8085) and exposes authenticated HTTP
endpoints for both CSI SAP2000 v25 OAPI and AutoCAD / DWG Title Block editing.

Endpoints:
  - GET  /health
      Returns availability & diagnostic status for both SAP2000 and AutoCAD/DWG.
  - POST /analyze  (alias: /sap2000/analyze)
      Accepts {"filename": "model.sdb", "file_b64": "...", "inputs": {...}}
      or {"model_path": "C:/...", "inputs": {...}}
      and returns the standardized Penny calculation result dict.
  - POST /sap2000/build-portal
      Accepts portal frame parameters (span_m, height_m, tributary_width_m,
      dead_kpa, live_kpa, fy_mpa) and returns the analysis result + base64
      encoded `.sdb` and `.s2k` files.
  - POST /autocad/update-title-block
      Accepts {"filename": "ECM Plan_ROAD.dwg", "file_b64": "...",
               "fields": {...}, "field_map": {...}}
      Updates the DWG using native AutoCAD COM (or Windows ODA+ezdxf), verifies
      the changes, and returns {"ok": bool, "message": str, "engine": str,
      "output_filename": str, "file_b64": str}.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import tempfile
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from engines import autocad_engine, sap2000_engine


def _git_commit() -> str:
    """Short commit hash of the checkout this file lives in ('' if not a git repo)."""
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


_RUNNING_COMMIT = _git_commit()

log = logging.getLogger("penny_win_worker.server")


class PennyWindowsWorkerHandler(BaseHTTPRequestHandler):
    """Unified HTTP handler for SAP2000 OAPI and AutoCAD/DWG operations."""

    keep_open: bool = True
    visible: bool = True
    auth_token: str = ""

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _check_auth(self) -> bool:
        if not self.auth_token:
            return True
        header = self.headers.get("Authorization", "")
        if header == f"Bearer {self.auth_token}":
            return True
        self._send_json(401, {"error": "Unauthorized"})
        return False

    def _read_json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            return json.loads(raw)
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON request body: {e}"})
            return None

    def do_GET(self) -> None:
        path = self.path.rstrip("/") or "/"
        if path == "/health":
            dwg_info = autocad_engine.get_dwg_engine_status()
            self._send_json(200, {
                "status": "ok",
                "service": "penny-win-worker",
                "sap2000_available": sap2000_engine.is_sap2000_available(),
                "dll_path": str(sap2000_engine.DEFAULT_DLL_PATH),
                # Commit this process loaded vs. what is checked out now. If they
                # differ, the worker was not restarted after `git pull`.
                "commit_running": _RUNNING_COMMIT,
                "commit_on_disk": _git_commit(),
                "restart_needed": _RUNNING_COMMIT != _git_commit(),
                **dwg_info,
            })
            return
        self._send_json(404, {"error": f"Unknown path: {self.path}"})

    def do_POST(self) -> None:
        if not self._check_auth():
            return

        path = self.path.rstrip("/")
        body = self._read_json_body()
        if body is None:
            return

        if path in ("/analyze", "/sap2000/analyze"):
            self._handle_sap2000_analyze(body)
        elif path == "/sap2000/build-portal":
            self._handle_sap2000_build_portal(body)
        elif path == "/autocad/update-title-block":
            self._handle_autocad_title_block(body)
        else:
            self._send_json(404, {"error": f"Unknown path: {self.path}"})

    def _handle_sap2000_analyze(self, body: dict[str, Any]) -> None:
        inputs = body.get("inputs") or {}
        filename = body.get("filename") or "model.sdb"
        file_b64 = body.get("file_b64")
        model_path = body.get("model_path")

        try:
            if file_b64:
                raw_bytes = base64.b64decode(file_b64)
                with tempfile.TemporaryDirectory(prefix="sap_upload_", ignore_cleanup_errors=True) as upload_dir:
                    safe_name = Path(filename).name
                    if not safe_name.lower().endswith((".sdb", ".s2k", ".$2k")):
                        safe_name += ".sdb"
                    tmp_model = Path(upload_dir) / safe_name
                    tmp_model.write_bytes(raw_bytes)
                    result = sap2000_engine.analyze_sap2000_model(
                        tmp_model,
                        inputs=inputs,
                        visible=self.visible,
                        keep_open=self.keep_open,
                    )
            elif model_path:
                result = sap2000_engine.analyze_sap2000_model(
                    model_path,
                    inputs=inputs,
                    visible=self.visible,
                    keep_open=self.keep_open,
                )
            else:
                self._send_json(400, {"error": "Provide either 'file_b64' or 'model_path'."})
                return

            self._send_json(200, result)
        except Exception as e:
            log.exception("SAP2000 worker analysis failed")
            self._send_json(500, {
                "error": str(e),
                "traceback": traceback.format_exc(),
            })

    def _handle_sap2000_build_portal(self, body: dict[str, Any]) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="sap_portal_", ignore_cleanup_errors=True) as tmp_dir:
                result = sap2000_engine.build_portal_frame_model(
                    span_m=float(body.get("span_m", 20.0)),
                    height_m=float(body.get("height_m", 20.0)),
                    tributary_width_m=float(body.get("tributary_width_m", 5.0)),
                    dead_kpa=float(body.get("dead_kpa", 10.0)),
                    live_kpa=float(body.get("live_kpa", 10.0)),
                    fy_mpa=float(body.get("fy_mpa", 355.0)),
                    output_dir=tmp_dir,
                    model_name=body.get("model_name"),
                    visible=self.visible,
                    keep_open=self.keep_open,
                )
                gen_files = result.get("generated_files", {})
                sdb_p = Path(gen_files.get("sdb_path", ""))
                s2k_p = Path(gen_files.get("s2k_path", ""))
                if sdb_p.is_file():
                    result["sdb_b64"] = base64.b64encode(sdb_p.read_bytes()).decode("ascii")
                    result["sdb_filename"] = sdb_p.name
                if s2k_p.is_file():
                    result["s2k_b64"] = base64.b64encode(s2k_p.read_bytes()).decode("ascii")
                    result["s2k_filename"] = s2k_p.name
                self._send_json(200, result)
        except Exception as e:
            log.exception("SAP2000 portal frame build failed")
            self._send_json(500, {
                "error": str(e),
                "traceback": traceback.format_exc(),
            })

    def _handle_autocad_title_block(self, body: dict[str, Any]) -> None:
        filename = body.get("filename") or "drawing.dwg"
        file_b64 = body.get("file_b64")
        fields = body.get("fields") or {}
        field_map = body.get("field_map") or {}

        if not file_b64:
            self._send_json(400, {"error": "Missing required field 'file_b64'."})
            return

        try:
            raw_bytes = base64.b64decode(file_b64)
            with tempfile.TemporaryDirectory(prefix="cad_upload_", ignore_cleanup_errors=True) as upload_dir:
                safe_name = Path(filename).name
                if not safe_name.lower().endswith(".dwg"):
                    safe_name += ".dwg"
                tmp_dwg = Path(upload_dir) / safe_name
                tmp_dwg.write_bytes(raw_bytes)

                ok, msg, out_path, engine_used = autocad_engine.update_dwg_title_block(
                    tmp_dwg,
                    fields=fields,
                    field_map=field_map,
                )

                resp_payload: dict[str, Any] = {
                    "ok": ok,
                    "message": msg,
                    "engine": engine_used,
                    "output_filename": out_path.name if out_path else safe_name,
                }
                if out_path and out_path.is_file():
                    resp_payload["file_b64"] = base64.b64encode(out_path.read_bytes()).decode("ascii")

                self._send_json(200, resp_payload)
        except Exception as e:
            log.exception("AutoCAD/DWG title block update failed")
            self._send_json(500, {
                "error": str(e),
                "traceback": traceback.format_exc(),
            })


def serve_worker(
    host: str = "0.0.0.0",
    port: int = 8085,
    visible: bool = True,
    keep_open: bool = True,
    token: str = "",
) -> None:
    """Start the unified Windows CAD/CAE HTTP worker server."""
    PennyWindowsWorkerHandler.visible = visible
    PennyWindowsWorkerHandler.keep_open = keep_open
    PennyWindowsWorkerHandler.auth_token = token
    server = ThreadingHTTPServer((host, port), PennyWindowsWorkerHandler)
    dwg_status = autocad_engine.get_dwg_engine_status()
    print(
        f"Penny Windows Worker listening on http://{host}:{port} "
        f"(SAP2000: {sap2000_engine.is_sap2000_available()}, "
        f"DWG Engine: {dwg_status['dwg_engine']}, keep_open={keep_open})",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Unified Windows SAP2000 & AutoCAD Worker for Penny COS Bot")
    parser.add_argument("--host", default=os.getenv("WORKER_HOST", os.getenv("SAP2000_WORKER_HOST", "0.0.0.0")))
    parser.add_argument("--port", type=int, default=int(os.getenv("WORKER_PORT", os.getenv("SAP2000_WORKER_PORT", "8085"))))
    parser.add_argument("--headless", action="store_true", help="Start SAP2000/AutoCAD with visible=False")
    # SAP2000 / AutoCAD stay open between jobs by default: starting them takes far
    # longer than the analysis or title-block edit itself.
    parser.add_argument("--keep-open", action="store_true", help="(default) keep SAP2000 / AutoCAD warm between requests")
    parser.add_argument("--close-after", action="store_true",
                        help="Close SAP2000 / AutoCAD after every request (old behaviour)")
    parser.add_argument("--token", default=os.getenv("WORKER_TOKEN", os.getenv("SAP2000_WORKER_TOKEN", "")), help="Optional Bearer token")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    serve_worker(
        host=args.host,
        port=args.port,
        visible=not args.headless,
        keep_open=not args.close_after and os.getenv("WORKER_KEEP_OPEN", "1") != "0",
        token=args.token,
    )
