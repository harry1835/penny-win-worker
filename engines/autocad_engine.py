"""
engines/autocad_engine.py — Native AutoCAD COM & ODA/ezdxf DWG Title Block Engine
=================================================================================
Updates DWG title block attributes (`AcDbBlockReference` / `INSERT`) and `MText` / `Text`
entities on the Windows VM using a two-tier strategy:

1. **Tier 1 — Native AutoCAD COM (`win32com.client` -> `AutoCAD.Application`):**
   Opens the `.dwg` file directly in AutoCAD on Windows, updates all matching
   Block Attributes, `AcDbMText`, and `AcDbText` across ModelSpace and PaperSpace
   Layouts, saves natively as `.dwg`, and verifies the changes in-memory.
2. **Tier 2 — Windows ODA File Converter + `ezdxf` (`ODAFileConverter.exe`):**
   If AutoCAD is not installed or COM initialization fails, converts `.dwg` -> `.dxf`
   using `ODAFileConverter`, edits via `ezdxf`, converts `.dxf` -> `.dwg`, and
   round-trip verifies that no values were lost.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from engines import app_keeper
from engines.process_guard import run_with_watchdog

log = logging.getLogger("penny_win_worker.autocad")

AUTOCAD_EXEC_TIMEOUT = int(os.getenv("AUTOCAD_EXEC_TIMEOUT", "180"))
# Leave AutoCAD running after a job: an ECM package has several DWGs and
# AutoCAD's start-up dominates each title-block edit otherwise.
AUTOCAD_KEEP_OPEN = os.getenv("AUTOCAD_KEEP_OPEN", "1") != "0"


class _AcadState:
    """AutoCAD instance this worker started (only that one is ever closed)."""
    owned = False
    last_used = 0.0


_ACAD_STATE = _AcadState()
_CAD_LOCK = threading.Lock()

# Default Windows installation paths for ODA File Converter
_WINDOWS_ODA_CANDIDATES = [
    os.getenv("ODA_CONVERTER_PATH", ""),
    r"C:\Program Files\ODA\ODAFileConverter 25.12.0\ODAFileConverter.exe",
    r"C:\Program Files\ODA\ODAFileConverter\ODAFileConverter.exe",
]


def find_oda_executable() -> str | None:
    """Locate ODAFileConverter binary in PATH or standard Windows install directories."""
    which_oda = shutil.which("ODAFileConverter")
    if which_oda:
        return which_oda
    for cand in _WINDOWS_ODA_CANDIDATES:
        if cand and Path(cand).exists():
            return str(Path(cand))
    # Glob Program Files\ODA if present on Windows
    oda_root = Path(r"C:\Program Files\ODA")
    if oda_root.exists():
        matches = sorted(oda_root.glob("**/ODAFileConverter.exe"), reverse=True)
        if matches:
            return str(matches[0])
    return None


def is_autocad_com_available() -> bool:
    """Return True if pywin32 is installed and AutoCAD ProgID is registered in Windows Registry."""
    if sys.platform != "win32":
        return False
    try:
        import winreg  # type: ignore[import-not-found]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "AutoCAD.Application"):
            return True
    except Exception:
        return False


def get_dwg_engine_status() -> dict[str, Any]:
    """Return diagnostic status of available DWG editing engines on this machine."""
    com_ok = is_autocad_com_available()
    oda_bin = find_oda_executable()
    oda_ok = oda_bin is not None
    try:
        import ezdxf  # noqa: F401
        ezdxf_ok = True
    except ImportError:
        ezdxf_ok = False

    if com_ok:
        active = "autocad_com"
    elif oda_ok and ezdxf_ok:
        active = "oda_ezdxf"
    else:
        active = "none"

    return {
        "autocad_available": active != "none",
        "dwg_engine": active,
        "autocad_com_registered": com_ok,
        "oda_executable": oda_bin,
        "ezdxf_installed": ezdxf_ok,
    }


def clean_caps(val: Any, key: str = "") -> str:
    """Format string for DWG title block:
    - Replace newlines with a space
    - Collapse multiple spaces into a single space
    - Strip leading/trailing whitespace
    - Convert to UPPERCASE, except email fields (`*_email`) which stay lowercase
    """
    if val is None:
        return ""
    if isinstance(val, datetime):
        return val.strftime("%d/%m/%Y")
    s = str(val).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower() if key.endswith("_email") else s.upper()


def _build_tag_map(fields: dict[str, str], field_map: dict[str, Any]) -> dict[str, str]:
    field_defs = field_map.get("fields", {})
    tag_to_val: dict[str, str] = {}
    for fkey, fval in fields.items():
        if not fval:
            continue
        cfg = field_defs.get(fkey, {})
        cleaned = clean_caps(fval, fkey)
        for tag in cfg.get("dwg_attribs", []):
            tag_to_val[tag.lower()] = cleaned
    return tag_to_val


def _transform_mtext_string(
    t: str,
    ins_y: float,
    fields: dict[str, str],
) -> tuple[bool, str]:
    """Apply Penny's ECM title-block MTEXT rules. Returns (changed, new_text)."""
    t_upper = t.upper()

    # NEVER touch Professional Engineer (Waytech) block
    if (
        "WAYTECH" in t_upper
        or "PEK CHUAN" in t_upper
        or "YANG DEXI" in t_upper
        or "STEVENYANGDX" in t_upper
        or "WAYTECHSUBMISSION" in t_upper
        or "PROFESSIONAL ENGINEER" in t_upper
        or ins_y < 120
    ):
        return False, t

    title_val = clean_caps(fields.get("project_title"))
    road_name = clean_caps(fields.get("road_name", ""))

    c_name = clean_caps(fields.get("contractor_name", ""))
    c_addr = clean_caps(fields.get("contractor_address", ""))
    c_cp = clean_caps(fields.get("contractor_cp", ""))
    c_tel = clean_caps(fields.get("contractor_phone", ""))
    c_mail = clean_caps(fields.get("contractor_email", ""), "contractor_email")

    cl_name = clean_caps(fields.get("client_name", ""))
    cl_addr = clean_caps(fields.get("client_address", ""))
    cl_cp = clean_caps(fields.get("client_cp", ""))
    cl_tel = clean_caps(fields.get("client_phone", ""))
    cl_mail = clean_caps(fields.get("client_email", ""), "client_email")

    if c_name and (
        "FAITHFUL VENTURES" in t_upper
        or "VINCLAW@" in t.lower()
        or "YAN CONSTRUCTION" in t_upper
        or "YANCONSTRUCTION" in t_upper
        or ("COMPANY ADDRESS:" in t_upper and "CONTACT PERSON:" in t_upper)
    ):
        return True, (
            f"{{\\fCalibri|b0|i0|c0|p34;\\H0.83333x;\\C256;{c_name}\\P"
            f"\\fArial|b0|i0|c0|p34;Company Address: \\fCalibri|b0|i0|c0|p34;{c_addr}\\P"
            f"\\fArial|b0|i0|c0|p34;Contact Person: \\fCalibri|b0|i0|c0|p34;{c_cp}\\P"
            f"\\fArial|b0|i0|c0|p34;Contact Number:\\fCalibri|b0|i0|c0|p34;{c_tel}\\P"
            f"\\fArial|b0|i0|c0|p34;Email: \\fCalibri|b0|i0|c0|p34;\\H1.1x;\\L{c_mail}}}"
        )

    if cl_name and (
        "LIAU MAY LING" in t_upper
        or "LIAUJ28@" in t.lower()
        or "MICHELLE TENG" in t_upper
        or ("ADDRESS:" in t_upper and "CONTACT NUMBER:" in t_upper and "NAME:" in t_upper)
    ):
        return True, (
            f"{{\\fCalibri|b0|i0|c0|p34;\\H0.83333x;\\C256;{cl_name}\\P"
            f"\\fArial|b0|i0|c0|p34;Address: \\fCalibri|b0|i0|c0|p34;{cl_addr}\\P"
            f"\\fArial|b0|i0|c0|p34;Name: \\fCalibri|b0|i0|c0|p34;{cl_cp}\\P"
            f"\\fArial|b0|i0|c0|p34;Contact Number:\\fCalibri|b0|i0|c0|p34;{cl_tel}\\P"
            f"\\fArial|b0|i0|c0|p34;Email: \\fCalibri|b0|i0|c0|p34;\\H1.1x;\\L{cl_mail}}}"
        )

    if title_val and (
        "PROPOSED NEW ERECTION" in t_upper
        or "PROPOSED ERECTION" in t_upper
        or "{{PROJECT_TITLE}}" in t
    ):
        return True, f"{{\\fTimes New Roman|b0|i0|c0|p18;\\H0.91667x;\\C256;{title_val}}}"

    if road_name and "WATTEN ESTATE ROAD" in t_upper:
        return True, t.replace("Watten Estate Road", road_name.title()).replace("WATTEN ESTATE ROAD", road_name.upper())

    return False, t


def _transform_text_string(val: str, fields: dict[str, str]) -> tuple[bool, str]:
    """Apply Penny's ECM title-block single-line TEXT rules. Returns (changed, new_text)."""
    val_upper = val.upper()
    ref_no = clean_caps(fields.get("ref_no"))
    road_name = clean_caps(fields.get("road_name", ""))

    if ref_no and ("A2085-01156" in val or "A2311-00029" in val or "A2550-" in val):
        return True, ref_no
    if road_name and val.startswith("WAY/"):
        return True, f"WAY/{road_name}/"
    if road_name and "WATTEN ESTATE ROAD" in val_upper:
        return True, val.replace("Watten Estate Road", road_name.title()).replace("WATTEN ESTATE ROAD", road_name.upper())
    return False, val


# ── Tier 1: Native AutoCAD COM Automation (Windows) ─────────────────────────

def update_dwg_via_autocad_com(
    dwg_path: Path,
    fields: dict[str, str],
    field_map: dict[str, Any],
    visible: bool = False,
) -> tuple[bool, str]:
    """Edit DWG directly in native AutoCAD via Windows COM (`AutoCAD.Application`)."""
    import pythoncom  # type: ignore[import-not-found]
    import win32com.client  # type: ignore[import-not-found]

    pythoncom.CoInitialize()
    try:
        try:
            acad = win32com.client.GetActiveObject("AutoCAD.Application")
        except Exception:
            acad = win32com.client.Dispatch("AutoCAD.Application")
            _ACAD_STATE.owned = True  # we started it, so we may close it

        try:
            acad.Visible = bool(visible)
        except Exception:
            pass

        doc = acad.Documents.Open(str(dwg_path.resolve()))
        modified = False
        tag_to_val = _build_tag_map(fields, field_map)
        seen_text: list[str] = []

        try:
            # Iterate ModelSpace + all PaperSpace Layouts
            spaces = [doc.ModelSpace]
            for i in range(doc.Layouts.Count):
                layout = doc.Layouts.Item(i)
                if layout.Name.upper() != "MODEL":
                    spaces.append(layout.Block)

            for space in spaces:
                for idx in range(space.Count):
                    ent = space.Item(idx)
                    obj_name = str(getattr(ent, "ObjectName", ""))

                    if obj_name == "AcDbBlockReference" and getattr(ent, "HasAttributes", False):
                        for att in ent.GetAttributes():
                            tag = str(att.TagString or "").lower()
                            if tag in tag_to_val:
                                att.TextString = tag_to_val[tag]
                                att.Update()
                                modified = True
                            seen_text.append(str(att.TextString or ""))

                    elif obj_name == "AcDbMText":
                        raw_text = str(ent.TextString or "")
                        ins_pt = getattr(ent, "InsertionPoint", (0.0, 999.0, 0.0))
                        ins_y = float(ins_pt[1]) if len(ins_pt) >= 2 else 999.0
                        changed, new_text = _transform_mtext_string(raw_text, ins_y, fields)
                        if changed:
                            ent.TextString = new_text
                            ent.Update()
                            modified = True
                        seen_text.append(str(ent.TextString or ""))

                    elif obj_name == "AcDbText":
                        raw_val = str(ent.TextString or "")
                        changed, new_val = _transform_text_string(raw_val, fields)
                        if changed:
                            ent.TextString = new_val
                            ent.Update()
                            modified = True
                        seen_text.append(str(ent.TextString or ""))

            if modified:
                # Read back what the drawing now says: a template whose text the
                # rules don't recognise must not pass with a green tick.
                missing = missing_values_in_text("\n".join(seen_text), fields)
                if missing:
                    return (
                        False,
                        f"⚠️ AutoCAD edit incomplete — {', '.join(missing[:3])} not found in the "
                        f"title block; original DWG preserved ({dwg_path.name})",
                    )
                doc.Save()
                return True, f"✅ title block updated natively in AutoCAD and verified ({dwg_path.name})"
            return (
                False,
                f"⚠️ nothing in the title block matched via AutoCAD COM — original DWG preserved ({dwg_path.name})",
            )
        finally:
            try:
                doc.Close(False)
            except Exception:
                pass
            # Kept open for the rest of the ECM package; app_keeper closes it
            # after AUTOCAD_IDLE_MIN idle minutes.
            if _ACAD_STATE.owned and not AUTOCAD_KEEP_OPEN:
                try:
                    acad.Quit()
                    _ACAD_STATE.owned = False
                except Exception:
                    pass
            _ACAD_STATE.last_used = time.monotonic()
    finally:
        pythoncom.CoUninitialize()


# ── Tier 2: ODA File Converter + ezdxf Round-Trip ───────────────────────────

def _run_cmd(cmd: list[str], timeout: int = 90) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:
        return -1, "", str(e)


def dwg_to_dxf_oda(dwg_path: Path, out_dir: Path, target_version: str = "ACAD2018") -> Path | None:
    oda_bin = find_oda_executable()
    if not oda_bin:
        return None
    cmd = [oda_bin, str(dwg_path.parent), str(out_dir), target_version, "DXF", "0", "1", dwg_path.name]
    if sys.platform != "win32" and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a"] + cmd
    code, stdout, stderr = _run_cmd(cmd)
    dxf_candidate = out_dir / f"{dwg_path.stem}.dxf"
    if dxf_candidate.exists():
        return dxf_candidate
    log.warning(f"ODA DWG->DXF failed (code {code}): {stderr or stdout}")
    return None


def dxf_to_dwg_oda(dxf_path: Path, out_dir: Path, target_version: str = "ACAD2018") -> Path | None:
    oda_bin = find_oda_executable()
    if not oda_bin:
        return None
    cmd = [oda_bin, str(dxf_path.parent), str(out_dir), target_version, "DWG", "0", "1", dxf_path.name]
    if sys.platform != "win32" and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a"] + cmd
    code, stdout, stderr = _run_cmd(cmd)
    dwg_candidate = out_dir / f"{dxf_path.stem}.dwg"
    if dwg_candidate.exists():
        return dwg_candidate
    log.warning(f"ODA DXF->DWG failed (code {code}): {stderr or stdout}")
    return None


def missing_values_in_text(text: str, fields: dict[str, str]) -> list[str]:
    """Field keys whose (cleaned) value does not appear in the drawing's text."""
    upper = text.upper()
    missing: list[str] = []
    for key in ("project_title", "ref_no", "client_name", "contractor_name"):
        probe = clean_caps(fields.get(key, ""))[:24].strip()
        if probe and probe not in upper:
            missing.append(key)
    return missing


def missing_values(dxf_path: Path, fields: dict[str, str]) -> list[str]:
    """Field keys whose value is absent from a DXF."""
    try:
        text = dxf_path.read_text(encoding="utf-8", errors="ignore").upper()
    except OSError:
        return []
    missing: list[str] = []
    for key in ("project_title", "ref_no", "client_name", "contractor_name"):
        value = clean_caps(fields.get(key, ""))
        probe = value[:24].strip()
        if probe and probe not in text:
            missing.append(key)
    return missing


def edit_dxf_title_block(dxf_path: Path, fields: dict[str, str], field_map: dict[str, Any]) -> bool:
    """Open DXF with ezdxf and set attribute / text values for title block tags."""
    try:
        import ezdxf
    except ImportError:
        log.error("ezdxf is not installed")
        return False

    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception as e:
        log.error(f"Failed to read DXF with ezdxf: {e}")
        return False

    msp = doc.modelspace()
    tag_to_val = _build_tag_map(fields, field_map)
    modified = False

    for block_ref in msp.query("INSERT"):
        for attrib in block_ref.attribs:
            tag = attrib.dxf.tag.lower()
            if tag in tag_to_val:
                attrib.dxf.text = tag_to_val[tag]
                modified = True

    for layout in doc.layouts:
        for block_ref in layout.query("INSERT"):
            for attrib in block_ref.attribs:
                tag = attrib.dxf.tag.lower()
                if tag in tag_to_val:
                    attrib.dxf.text = tag_to_val[tag]
                    modified = True

    all_spaces = [msp] + [l for l in doc.layouts if l.name != "Model"]
    for space in all_spaces:
        for mtext in space.query("MTEXT"):
            ins_y = getattr(mtext.dxf, "insert", (0, 999))[1]
            changed, new_text = _transform_mtext_string(mtext.text, ins_y, fields)
            if changed:
                mtext.text = new_text
                modified = True

        for tx in space.query("TEXT"):
            changed, new_val = _transform_text_string(tx.dxf.text, fields)
            if changed:
                tx.dxf.text = new_val
                modified = True

    try:
        doc.save()
        return modified
    except Exception as e:
        log.error(f"Failed to save DXF: {e}")
        return False


def _update_dwg_title_block_inner(
    dwg_path: Path,
    fields: dict[str, str],
    field_map: dict[str, Any],
) -> tuple[bool, str, Path | None, str]:
    """Returns (ok, message, output_path, engine_used)."""
    if not dwg_path.exists():
        return False, f"⚠️ DWG file not found: {dwg_path.name}", None, "none"

    # Tier 1: Try native AutoCAD COM if available on Windows
    com_error = ""
    if is_autocad_com_available():
        try:
            ok, msg = update_dwg_via_autocad_com(dwg_path, fields, field_map)
            return ok, msg, dwg_path, "autocad_com"
        except Exception as e:
            com_error = f"{type(e).__name__}: {e}"
            log.warning(f"AutoCAD COM failed on {dwg_path.name} ({com_error}), falling back to ODA+ezdxf...")

    # Tier 2: ODA File Converter + ezdxf
    if not find_oda_executable():
        reason = (
            f"AutoCAD COM failed ({com_error[:160]}) and no ODA File Converter on worker"
            if com_error else
            "AutoCAD.Application is not registered and no ODA File Converter on worker"
        )
        return False, f"⚠️ untouched ({reason})", dwg_path, "none"

    with tempfile.TemporaryDirectory(prefix="penny_oda_") as tmpdir:
        tmp = Path(tmpdir)
        step1_dir = tmp / "step1"
        step2_dir = tmp / "step2"
        step1_dir.mkdir()
        step2_dir.mkdir()

        dxf_file = dwg_to_dxf_oda(dwg_path, step1_dir)
        if not dxf_file or not dxf_file.exists():
            return (
                False,
                f"⚠️ DWG->DXF conversion failed — original DWG preserved ({dwg_path.name})",
                dwg_path,
                "oda_ezdxf",
            )

        if not edit_dxf_title_block(dxf_file, fields, field_map):
            return (
                False,
                f"⚠️ nothing in the title block matched — original DWG preserved "
                f"({dwg_path.name}); the match rules likely do not fit this template",
                dwg_path,
                "oda_ezdxf",
            )

        new_dwg = dxf_to_dwg_oda(dxf_file, step2_dir)
        if not new_dwg or not new_dwg.exists():
            fallback_dxf = dwg_path.with_suffix(".dxf")
            shutil.copy2(dxf_file, fallback_dxf)
            return (
                True,
                f"⚠️ DXF->DWG conversion failed — exported edited DXF alongside untouched DWG ({fallback_dxf.name})",
                fallback_dxf,
                "oda_ezdxf",
            )

        verify_dir = tmp / "verify"
        verify_dir.mkdir()
        verify_dxf = dwg_to_dxf_oda(new_dwg, verify_dir)
        if verify_dxf and verify_dxf.exists():
            missing = missing_values(verify_dxf, fields)
            if missing:
                fallback_dxf = dwg_path.with_suffix(".dxf")
                shutil.copy2(dxf_file, fallback_dxf)
                return (
                    False,
                    f"⚠️ round-trip lost {len(missing)} value(s) ({', '.join(missing[:3])}) — "
                    f"original DWG preserved, edited DXF exported as {fallback_dxf.name}",
                    fallback_dxf,
                    "oda_ezdxf",
                )
            shutil.copy2(new_dwg, dwg_path)
            return True, f"✅ title block updated and verified ({dwg_path.name})", dwg_path, "oda_ezdxf"
        else:
            fallback_dxf = dwg_path.with_suffix(".dxf")
            shutil.copy2(dxf_file, fallback_dxf)
            return (
                True,
                f"⚠️ verification failed — exported edited DXF alongside untouched DWG ({fallback_dxf.name})",
                fallback_dxf,
                "oda_ezdxf",
            )


def update_dwg_title_block(
    dwg_path: Path,
    fields: dict[str, str],
    field_map: dict[str, Any],
    timeout_seconds: int | None = None,
) -> tuple[bool, str, Path | None, str]:
    """Thread-safe, watchdog-guarded DWG title block updater."""
    timeout = timeout_seconds if timeout_seconds is not None else AUTOCAD_EXEC_TIMEOUT
    with _CAD_LOCK:
        return run_with_watchdog(
            _update_dwg_title_block_inner,
            dwg_path,
            fields,
            field_map,
            timeout_seconds=timeout,
            kill_on_timeout=["acad.exe", "accoreconsole.exe", "ODAFileConverter.exe"],
        )


def reap_idle_autocad(now: float | None = None) -> bool:
    """Quit the worker-started AutoCAD after AUTOCAD_IDLE_MIN idle minutes.

    Never while a drawing is open in it (someone may be working in it)."""
    if not _ACAD_STATE.owned:
        return False
    if app_keeper.idle_for(_ACAD_STATE.last_used, now) < app_keeper.autocad_idle_limit_s():
        return False
    if not _CAD_LOCK.acquire(blocking=False):
        return False
    try:
        import pythoncom  # type: ignore[import-not-found]
        import win32com.client  # type: ignore[import-not-found]

        pythoncom.CoInitialize()
        try:
            try:
                acad = win32com.client.GetActiveObject("AutoCAD.Application")
            except Exception:
                _ACAD_STATE.owned = False  # already gone
                return False
            if int(acad.Documents.Count) > 0:
                return False
            acad.Quit()
            _ACAD_STATE.owned = False
            log.info("Closed idle AutoCAD")
            return True
        finally:
            pythoncom.CoUninitialize()
    finally:
        _CAD_LOCK.release()
