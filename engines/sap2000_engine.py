"""
engines/sap2000_engine.py — SAP2000 v25 OAPI Analysis & Portal Frame Engine
===========================================================================
Drives CSI SAP2000 v25 on Windows via pythonnet (`clr.AddReference("SAP2000v1.dll")`).
Provides:
  - `is_sap2000_available()`
  - `analyze_sap2000_model()`
  - `build_portal_frame_model()`
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from engines.process_guard import run_with_watchdog

log = logging.getLogger("penny_win_worker.sap2000")

DEFAULT_SAP_DIR = Path(os.getenv(
    "SAP2000_DIR",
    r"C:\Program Files\Computers and Structures\SAP2000 25",
))
DEFAULT_DLL_PATH = DEFAULT_SAP_DIR / "SAP2000v1.dll"
SAP2000_EXEC_TIMEOUT = int(os.getenv("SAP2000_EXEC_TIMEOUT", "280"))

# Serialize SAP2000 OAPI calls so concurrent requests never clobber COM state
_SAP_LOCK = threading.Lock()


def is_sap2000_available() -> bool:
    """Return True if SAP2000v1.dll exists on this machine."""
    return DEFAULT_DLL_PATH.exists()


def _load_sap_module():
    """Dynamically load SAP2000v1.dll via pythonnet (clr)."""
    if not DEFAULT_DLL_PATH.exists():
        raise FileNotFoundError(
            f"SAP2000v1.dll not found at {DEFAULT_DLL_PATH}. "
            "Set SAP2000_DIR environment variable if installed elsewhere."
        )
    import clr  # type: ignore[import-not-found]
    clr.AddReference(str(DEFAULT_DLL_PATH))
    import SAP2000v1 as sap  # type: ignore[import-not-found]
    return sap


def _init_com_thread() -> None:
    """Initialize Windows COM apartment on the current worker thread if pythoncom is installed."""
    try:
        import pythoncom  # type: ignore[import-not-found]
        pythoncom.CoInitialize()
    except Exception:
        pass


def _uninit_com_thread() -> None:
    try:
        import pythoncom  # type: ignore[import-not-found]
        pythoncom.CoUninitialize()
    except Exception:
        pass


def _analyze_sap2000_model_inner(
    model_path: str | Path,
    inputs: dict[str, Any] | None = None,
    visible: bool = True,
    keep_open: bool = False,
) -> dict[str, Any]:
    inputs = dict(inputs or {})
    src_path = Path(model_path).resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"SAP2000 model file not found: {src_path}")

    _init_com_thread()
    try:
        sap = _load_sap_module()

        with tempfile.TemporaryDirectory(prefix="penny_sap2000_", ignore_cleanup_errors=True) as tmp_dir:
            tmp_dir_path = Path(tmp_dir)
            work_model_path = tmp_dir_path / src_path.name
            shutil.copy2(src_path, work_model_path)

            helper_iface = sap.cHelper(sap.Helper())
            sap_obj = helper_iface.GetObject("CSI.SAP2000.API.SapObject")
            attached = True
            if sap_obj is None:
                attached = False
                sap_obj = helper_iface.CreateObjectProgID("CSI.SAP2000.API.SapObject")
                ret = sap_obj.ApplicationStart(sap.eUnits.kN_m_C, visible)
                if ret != 0:
                    raise RuntimeError(f"SAP2000 ApplicationStart failed with code {ret}")

            try:
                model = sap_obj.SapModel
                ret = model.File.OpenFile(str(work_model_path))
                if ret != 0:
                    raise RuntimeError(f"SAP2000 File.OpenFile({work_model_path.name}) failed with code {ret}")

                # Ensure present working units are kN, m, C
                model.SetPresentUnits(sap.eUnits.kN_m_C)

                num_joints = int(model.PointObj.Count())
                num_frames = int(model.FrameObj.Count())
                num_areas = int(model.AreaObj.Count())

                # Frame section mapping: frame_name -> section_name
                frame_sections: dict[str, str] = {}
                ret_fnames = model.FrameObj.GetNameList(0, [])
                frame_names = list(ret_fnames[2]) if (len(ret_fnames) >= 3 and ret_fnames[2]) else []
                for fname in frame_names:
                    sec_res = model.FrameObj.GetSection(fname, "", "")
                    if sec_res[0] == 0 and len(sec_res) >= 2:
                        frame_sections[fname] = str(sec_res[1])

                # Run structural analysis
                ret = model.Analyze.RunAnalysis()
                if ret != 0:
                    raise RuntimeError(f"SAP2000 Analyze.RunAnalysis() failed with code {ret}")

                # Discover load cases and load combinations
                ret_lcases = model.LoadCases.GetNameList(0, [])
                load_cases = list(ret_lcases[2]) if (len(ret_lcases) >= 3 and ret_lcases[2]) else []

                ret_combos = model.RespCombo.GetNameList(0, [])
                load_combos = list(ret_combos[2]) if (len(ret_combos) >= 3 and ret_combos[2]) else []

                # Select all load cases and combinations for output extraction
                model.Results.Setup.DeselectAllCasesAndCombosForOutput()
                for lc in load_cases:
                    model.Results.Setup.SetCaseSelectedForOutput(lc)
                for cb in load_combos:
                    model.Results.Setup.SetComboSelectedForOutput(cb)

                # ── 1. Joint Displacements ──────────────────────────────────────
                res_displ = model.Results.JointDispl(
                    "ALL", sap.eItemTypeElm.GroupElm,
                    0, [], [], [], [], [], [], [], [], [], [], []
                )
                n_displ = int(res_displ[1]) if res_displ[0] == 0 else 0
                max_u1_mm = 0.0
                max_u2_mm = 0.0
                max_u3_mm = 0.0
                max_disp_mm = 0.0
                gov_disp_joint = "–"
                gov_disp_case = "–"

                joint_disp_map: dict[str, dict[str, Any]] = {}
                for i in range(n_displ):
                    j_name = str(res_displ[2][i])
                    lc_name = str(res_displ[4][i])
                    u1_mm = float(res_displ[7][i]) * 1000.0
                    u2_mm = float(res_displ[8][i]) * 1000.0
                    u3_mm = float(res_displ[9][i]) * 1000.0
                    mag_mm = max(abs(u1_mm), abs(u2_mm), abs(u3_mm))

                    if abs(u1_mm) > abs(max_u1_mm):
                        max_u1_mm = u1_mm
                    if abs(u2_mm) > abs(max_u2_mm):
                        max_u2_mm = u2_mm
                    if abs(u3_mm) > abs(max_u3_mm):
                        max_u3_mm = u3_mm
                    if mag_mm > max_disp_mm:
                        max_disp_mm = mag_mm
                        gov_disp_joint = j_name
                        gov_disp_case = lc_name

                    prev = joint_disp_map.get(j_name)
                    if prev is None or mag_mm > prev["max_mm"]:
                        joint_disp_map[j_name] = {
                            "joint": j_name,
                            "case": lc_name,
                            "u1_mm": round(u1_mm, 3),
                            "u2_mm": round(u2_mm, 3),
                            "u3_mm": round(u3_mm, 3),
                            "max_mm": round(mag_mm, 3),
                        }

                joint_displacements = sorted(
                    joint_disp_map.values(),
                    key=lambda x: x["max_mm"],
                    reverse=True,
                )[:15]

                # ── 2. Support / Base Reactions ─────────────────────────────────
                res_react = model.Results.JointReact(
                    "ALL", sap.eItemTypeElm.GroupElm,
                    0, [], [], [], [], [], [], [], [], [], [], []
                )
                n_react = int(res_react[1]) if res_react[0] == 0 else 0
                max_f1 = 0.0
                max_f2 = 0.0
                max_f3 = 0.0
                max_m_support = 0.0

                support_map: dict[str, dict[str, Any]] = {}
                for i in range(n_react):
                    j_name = str(res_react[2][i])
                    lc_name = str(res_react[4][i])
                    f1 = float(res_react[7][i])
                    f2 = float(res_react[8][i])
                    f3 = float(res_react[9][i])
                    m1 = float(res_react[10][i])
                    m2 = float(res_react[11][i])
                    m3 = float(res_react[12][i])

                    if abs(f1) > abs(max_f1):
                        max_f1 = f1
                    if abs(f2) > abs(max_f2):
                        max_f2 = f2
                    if abs(f3) > abs(max_f3):
                        max_f3 = f3
                    m_gov = max(abs(m1), abs(m2), abs(m3))
                    if m_gov > abs(max_m_support):
                        max_m_support = m_gov

                    mag_f = (f1**2 + f2**2 + f3**2) ** 0.5
                    if mag_f < 1e-6 and m_gov < 1e-6:
                        continue
                    prev = support_map.get(j_name)
                    if prev is None or mag_f > prev["_mag"]:
                        support_map[j_name] = {
                            "joint": j_name,
                            "case": lc_name,
                            "F1_kN": round(f1, 2),
                            "F2_kN": round(f2, 2),
                            "F3_kN": round(f3, 2),
                            "M1_kNm": round(m1, 2),
                            "M2_kNm": round(m2, 2),
                            "M3_kNm": round(m3, 2),
                            "_mag": mag_f,
                        }

                support_reactions = []
                for item in sorted(support_map.values(), key=lambda x: x["_mag"], reverse=True)[:15]:
                    clean_item = {k: v for k, v in item.items() if k != "_mag"}
                    support_reactions.append(clean_item)

                # ── 3. Frame Forces & Member Envelopes ──────────────────────────
                res_frame = model.Results.FrameForce(
                    "ALL", sap.eItemTypeElm.GroupElm,
                    0, [], [], [], [], [], [], [], [], [], [], [], [], []
                )
                n_frame = int(res_frame[1]) if res_frame[0] == 0 else 0
                max_p = 0.0
                max_v2 = 0.0
                max_v3 = 0.0
                max_t = 0.0
                max_m2 = 0.0
                max_m3 = 0.0

                member_env_map: dict[str, dict[str, Any]] = {}
                for i in range(n_frame):
                    f_name = str(res_frame[2][i])
                    lc_name = str(res_frame[6][i])
                    p = float(res_frame[9][i])
                    v2 = float(res_frame[10][i])
                    v3 = float(res_frame[11][i])
                    t = float(res_frame[12][i])
                    m2 = float(res_frame[13][i])
                    m3 = float(res_frame[14][i])

                    if abs(p) > abs(max_p):
                        max_p = p
                    if abs(v2) > abs(max_v2):
                        max_v2 = v2
                    if abs(v3) > abs(max_v3):
                        max_v3 = v3
                    if abs(t) > abs(max_t):
                        max_t = t
                    if abs(m2) > abs(max_m2):
                        max_m2 = m2
                    if abs(m3) > abs(max_m3):
                        max_m3 = m3

                    env = member_env_map.get(f_name)
                    if env is None:
                        env = {
                            "frame": f_name,
                            "section": frame_sections.get(f_name, "–"),
                            "P_max_kN": 0.0,
                            "V2_max_kN": 0.0,
                            "M3_max_kNm": 0.0,
                            "M2_max_kNm": 0.0,
                            "T_max_kNm": 0.0,
                            "gov_case": lc_name,
                            "_score": 0.0,
                        }
                        member_env_map[f_name] = env

                    if abs(p) > abs(env["P_max_kN"]):
                        env["P_max_kN"] = round(p, 2)
                    if abs(v2) > abs(env["V2_max_kN"]):
                        env["V2_max_kN"] = round(v2, 2)
                    if abs(m3) > abs(env["M3_max_kNm"]):
                        env["M3_max_kNm"] = round(m3, 2)
                        env["gov_case"] = lc_name
                    if abs(m2) > abs(env["M2_max_kNm"]):
                        env["M2_max_kNm"] = round(m2, 2)
                    if abs(t) > abs(env["T_max_kNm"]):
                        env["T_max_kNm"] = round(t, 2)
                    score = abs(env["M3_max_kNm"]) + abs(env["V2_max_kN"]) + abs(env["P_max_kN"])
                    if score > env["_score"]:
                        env["_score"] = score
                        env["gov_case"] = lc_name

                member_envelopes = []
                for item in sorted(member_env_map.values(), key=lambda x: x["_score"], reverse=True)[:20]:
                    clean_item = {k: v for k, v in item.items() if k != "_score"}
                    member_envelopes.append(clean_item)

                # ── 4. Optional Steel Frame Code Design (DesignSteel) ───────────
                steel_design_results: list[dict[str, Any]] = []
                max_steel_ratio = 0.0
                gov_steel_frame = "–"
                gov_steel_combo = "–"
                if inputs.get("run_steel_design", True) and num_frames > 0:
                    try:
                        ret_des = model.DesignSteel.StartDesign()
                        if ret_des == 0:
                            res_sum = model.DesignSteel.GetSummaryResults(
                                "ALL", 0, [], [], [], [], [], [], [], sap.eItemType.Group
                            )
                            if res_sum[0] == 0 and int(res_sum[1]) > 0:
                                n_des = int(res_sum[1])
                                for i in range(n_des):
                                    f_name = str(res_sum[2][i])
                                    ratio = float(res_sum[3][i])
                                    loc_m = float(res_sum[5][i])
                                    combo = str(res_sum[6][i])
                                    err_sum = str(res_sum[7][i] or "")
                                    warn_sum = str(res_sum[8][i] or "")
                                    if ratio > max_steel_ratio:
                                        max_steel_ratio = ratio
                                        gov_steel_frame = f_name
                                        gov_steel_combo = combo
                                    steel_design_results.append({
                                        "frame": f_name,
                                        "section": frame_sections.get(f_name, "–"),
                                        "combo": f"{combo} (@{loc_m:.1f}m)",
                                        "ratio": round(ratio, 3),
                                        "status": "PASS" if (ratio <= 1.0 and not err_sum) else "FAIL",
                                        "notes": (err_sum or warn_sum or "OK").strip(),
                                    })
                                steel_design_results.sort(key=lambda x: x["ratio"], reverse=True)
                    except Exception as e:
                        log.debug(f"DesignSteel skipped or not configured in model: {e}")

            finally:
                if not attached and not keep_open:
                    try:
                        sap_obj.ApplicationExit(False)
                    except Exception:
                        pass
                else:
                    try:
                        model.File.NewBlank()
                    except Exception:
                        pass
    finally:
        _uninit_com_thread()

    # ── 5. Build Engineering Checks ─────────────────────────────────────────
    disp_limit_mm = float(inputs.get("deflection_limit_mm") or 25.0)
    design_checked = bool(steel_design_results)

    checks: list[dict[str, Any]] = []

    if design_checked:
        checks.append({
            "name": f"Steel Code Check (Frame {gov_steel_frame}, {gov_steel_combo})",
            "demand": f"{max_steel_ratio:.3f}",
            "capacity": "1.000",
            "util": round(max_steel_ratio, 3),
            "pass": max_steel_ratio <= 1.0,
        })

    disp_util = round(max_disp_mm / disp_limit_mm, 3) if disp_limit_mm > 0 else 0.0
    checks.append({
        "name": f"Max Nodal Deflection (joints only, Joint {gov_disp_joint}, Case {gov_disp_case})",
        "demand": f"{max_disp_mm:.2f} mm",
        "capacity": f"{disp_limit_mm:.2f} mm",
        "util": disp_util,
        "pass": max_disp_mm <= disp_limit_mm,
    })

    overall_pass: bool | None = all(c["pass"] for c in checks) if design_checked else None
    if design_checked:
        overall_status = "PASS ✅" if overall_pass else "FAIL ❌"
    else:
        overall_status = "ANALYSIS ONLY – NO STEEL DESIGN CHECK"
    governing_util = max((c["util"] for c in checks), default=0.0)

    resolved_inputs = {
        "project": inputs.get("project", "Structural Analysis Model"),
        "description": inputs.get("description", f"SAP2000 Structural Analysis ({src_path.name})"),
        "model_file": src_path.name,
        "units": "kN, m, °C",
        "num_joints": num_joints,
        "num_frames": num_frames,
        "num_areas": num_areas,
        "load_cases": ", ".join(load_cases) if load_cases else "None",
        "load_combos": ", ".join(load_combos) if load_combos else "None",
        "deflection_limit_mm": disp_limit_mm,
    }

    return {
        "engine": "sap2000",
        "inputs": resolved_inputs,
        "overall_pass": overall_pass,
        "overall_status": overall_status,
        "design_checked": design_checked,
        "governing_util": round(governing_util, 3),
        "checks": checks,
        "summary": {
            "max_disp_mm": round(max_disp_mm, 3),
            "max_u1_mm": round(max_u1_mm, 3),
            "max_u2_mm": round(max_u2_mm, 3),
            "max_u3_mm": round(max_u3_mm, 3),
            "max_f1_kN": round(max_f1, 2),
            "max_f2_kN": round(max_f2, 2),
            "max_f3_kN": round(max_f3, 2),
            "max_m_support_kNm": round(max_m_support, 2),
            "max_p_kN": round(max_p, 2),
            "max_v2_kN": round(max_v2, 2),
            "max_v3_kN": round(max_v3, 2),
            "max_t_kNm": round(max_t, 2),
            "max_m2_kNm": round(max_m2, 2),
            "max_m3_kNm": round(max_m3, 2),
            "max_steel_ratio": round(max_steel_ratio, 3),
        },
        "member_envelopes": member_envelopes,
        "support_reactions": support_reactions,
        "joint_displacements": joint_displacements,
        "steel_design_results": steel_design_results[:20],
    }


def analyze_sap2000_model(
    model_path: str | Path,
    inputs: dict[str, Any] | None = None,
    visible: bool = True,
    keep_open: bool = False,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Thread-safe, watchdog-guarded SAP2000 model analysis."""
    timeout = timeout_seconds if timeout_seconds is not None else SAP2000_EXEC_TIMEOUT
    with _SAP_LOCK:
        return run_with_watchdog(
            _analyze_sap2000_model_inner,
            model_path,
            inputs=inputs,
            visible=visible,
            keep_open=keep_open,
            timeout_seconds=timeout,
            kill_on_timeout=["SAP2000.exe"],
        )


def build_portal_frame_model(
    span_m: float = 20.0,
    height_m: float = 20.0,
    tributary_width_m: float = 5.0,
    dead_kpa: float = 10.0,
    live_kpa: float = 10.0,
    fy_mpa: float = 355.0,
    output_dir: str | Path | None = None,
    model_name: str | None = None,
    visible: bool = True,
    keep_open: bool = False,
) -> dict[str, Any]:
    """
    Build a 2D Steel Portal Frame from scratch in SAP2000 v25 via OAPI,
    auto-size the section under Eurocode 3-2005 (`SS EN 1993-1-1`), save both
    `.sdb` and `.s2k` (`$2k`) files, and return the full analysis result dict.
    """
    _init_com_thread()
    try:
        sap = _load_sap_module()
        out_dir = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix="penny_portal_"))
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = model_name or f"Portal_Frame_{int(span_m)}x{int(height_m)}_EC3"
        sdb_path = out_dir / f"{stem}.sdb"
        s2k_path = out_dir / f"{stem}.s2k"
        dollar_2k_path = out_dir / f"{stem}.$2k"

        gk_kn_m = round(dead_kpa * tributary_width_m, 3)
        qk_kn_m = round(live_kpa * tributary_width_m, 3)

        with _SAP_LOCK:
            helper_iface = sap.cHelper(sap.Helper())
            sap_obj = helper_iface.GetObject("CSI.SAP2000.API.SapObject")
            attached = True
            if sap_obj is None:
                attached = False
                sap_obj = helper_iface.CreateObjectProgID("CSI.SAP2000.API.SapObject")
                ret = sap_obj.ApplicationStart(sap.eUnits.kN_m_C, visible)
                if ret != 0:
                    raise RuntimeError(f"SAP2000 ApplicationStart failed with code {ret}")

            try:
                model = sap_obj.SapModel
                model.InitializeNewModel(sap.eUnits.kN_m_C)
                model.File.NewBlank()
                model.SetPresentUnits(sap.eUnits.kN_m_C)

                mat_name = f"S{int(fy_mpa)}_EC3"
                model.PropMaterial.SetMaterial(mat_name, sap.eMatType.Steel)
                model.PropMaterial.SetMPIsotropic(mat_name, 2.1e8, 0.3, 1.2e-5)
                fy_kn_m2 = fy_mpa * 1000.0
                fu_kn_m2 = max(fy_mpa * 1.4, 510.0) * 1000.0
                model.PropMaterial.SetOSteel_1(
                    mat_name, fy_kn_m2, fu_kn_m2, fy_kn_m2, fu_kn_m2, 1, 1, 0.015, 0.11, 0.20, -0.1
                )
                model.PropMaterial.SetWeightAndMass(mat_name, 1, 77.0)

                candidates = [
                    ("UB-610x305x179", 0.620, 0.307, 0.0236, 0.0141),
                    ("UB-914x419x388", 0.921, 0.420, 0.0366, 0.0214),
                    ("PG-1200x450x40x25", 1.20, 0.45, 0.040, 0.025),
                    ("PG-1400x500x45x28", 1.40, 0.50, 0.045, 0.028),
                    ("PG-1600x550x50x30", 1.60, 0.55, 0.050, 0.030),
                    ("PG-1800x600x55x32", 1.80, 0.60, 0.055, 0.032),
                    ("PG-2000x650x60x35", 2.00, 0.65, 0.060, 0.035),
                ]
                for sec_name, d, bf, tf, tw in candidates:
                    model.PropFrame.SetISection(sec_name, mat_name, d, bf, tf, tw, bf, tf)

                chosen_sec = candidates[0][0]

                model.PointObj.AddCartesian(0.0, 0.0, 0.0, "", "1")
                model.PointObj.AddCartesian(0.0, 0.0, float(height_m), "", "2")
                model.PointObj.AddCartesian(float(span_m), 0.0, float(height_m), "", "3")
                model.PointObj.AddCartesian(float(span_m), 0.0, 0.0, "", "4")

                model.PointObj.SetRestraint("1", [True, True, True, True, True, True])
                model.PointObj.SetRestraint("4", [True, True, True, True, True, True])
                model.PointObj.SetRestraint("2", [False, True, False, True, False, True])
                model.PointObj.SetRestraint("3", [False, True, False, True, False, True])

                model.FrameObj.AddByPoint("1", "2", "", chosen_sec, "C1_LeftCol")
                model.FrameObj.AddByPoint("2", "3", "", chosen_sec, "B1_Rafter")
                model.FrameObj.AddByPoint("4", "3", "", chosen_sec, "C2_RightCol")

                ret_fnames = model.FrameObj.GetNameList(0, [])
                fnames = list(ret_fnames[2])
                rafter_name = fnames[1]

                model.LoadPatterns.Add("DEAD", sap.eLoadPatternType.Dead, 1.0, True)
                model.LoadPatterns.Add("LIVE", sap.eLoadPatternType.Live, 0.0, True)
                model.FrameObj.SetLoadDistributed(rafter_name, "DEAD", 1, 10, 0.0, 1.0, gk_kn_m, gk_kn_m, "Global", True, True)
                model.FrameObj.SetLoadDistributed(rafter_name, "LIVE", 1, 10, 0.0, 1.0, qk_kn_m, qk_kn_m, "Global", True, True)

                model.RespCombo.Add("ULS_EC3", 0)
                model.RespCombo.SetCaseList("ULS_EC3", sap.eCNameType.LoadCase, "DEAD", 1.35)
                model.RespCombo.SetCaseList("ULS_EC3", sap.eCNameType.LoadCase, "LIVE", 1.50)

                model.RespCombo.Add("SLS_CHAR", 0)
                model.RespCombo.SetCaseList("SLS_CHAR", sap.eCNameType.LoadCase, "DEAD", 1.00)
                model.RespCombo.SetCaseList("SLS_CHAR", sap.eCNameType.LoadCase, "LIVE", 1.00)

                model.DesignSteel.SetCode("Eurocode 3-2005")
                model.DesignSteel.SetComboAutoGenerate(False)
                model.DesignSteel.SetComboStrength("ULS_EC3", True)
                model.DesignSteel.SetComboDeflection("SLS_CHAR", True)

                for sec_name, d, bf, tf, tw in candidates:
                    model.SetModelIsLocked(False)
                    model.Analyze.SetRunCaseFlag("", False, True)
                    model.Analyze.SetRunCaseFlag("DEAD", True, False)
                    model.Analyze.SetRunCaseFlag("LIVE", True, False)
                    for fn in fnames:
                        model.FrameObj.SetSection(fn, sec_name)
                        model.DesignSteel.Eurocode_3_2005.SetOverwrite(fn, 19, 0.25)
                        model.DesignSteel.Eurocode_3_2005.SetOverwrite(fn, 17, 0.25)

                    model.File.Save(str(sdb_path))
                    model.Analyze.RunAnalysis()
                    model.DesignSteel.StartDesign()

                    res_sum = model.DesignSteel.GetSummaryResults("ALL", 0, [], [], [], [], [], [], [], sap.eItemType.Group)
                    ratios = list(res_sum[3]) if (res_sum[0] == 0 and int(res_sum[1]) > 0) else []
                    max_ratio = max(ratios) if ratios else 999.0
                    chosen_sec = sec_name
                    if max_ratio <= 1.0:
                        break

                model.File.Save(str(sdb_path))
                if dollar_2k_path.exists():
                    shutil.copy2(dollar_2k_path, s2k_path)
            finally:
                if not attached and not keep_open:
                    try:
                        sap_obj.ApplicationExit(False)
                    except Exception:
                        pass
    finally:
        _uninit_com_thread()

    inputs = {
        "project": f"EC3-PORTAL-{int(span_m)}x{int(height_m)}",
        "description": (
            f"{span_m:g}m Span × {height_m:g}m Height Fixed-Base Steel Portal Frame ({chosen_sec}, S{int(fy_mpa)}) — "
            f"Tributary Width = {tributary_width_m:g}m, GK = {dead_kpa:g} kPa ({gk_kn_m:g} kN/m), "
            f"QK = {live_kpa:g} kPa ({qk_kn_m:g} kN/m), Eurocode 3"
        ),
        "deflection_limit_mm": round(span_m * 1000.0 / 200.0, 1),
        "run_steel_design": True,
    }
    res = analyze_sap2000_model(sdb_path, inputs=inputs, visible=visible, keep_open=keep_open)
    res["generated_files"] = {
        "sdb_path": str(sdb_path),
        "s2k_path": str(s2k_path) if s2k_path.exists() else str(dollar_2k_path),
        "dollar_2k_path": str(dollar_2k_path),
    }
    return res
