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
import re
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from engines import app_keeper, sap2000_post
from engines.process_guard import run_with_watchdog

log = logging.getLogger("penny_win_worker.sap2000")

DEFAULT_SAP_DIR = Path(os.getenv(
    "SAP2000_DIR",
    r"C:\Program Files\Computers and Structures\SAP2000 25",
))
DEFAULT_DLL_PATH = DEFAULT_SAP_DIR / "SAP2000v1.dll"
SAP2000_EXEC_TIMEOUT = int(os.getenv("SAP2000_EXEC_TIMEOUT", "480"))  # cold start alone ~170 s
# Load-case types whose solution needs the modal case (response spectrum,
# modal time history / steady state / PSD). Modal is only skipped without them.
_NEEDS_MODAL_TYPES = {4, 5, 7, 11, 12}

# Serialize SAP2000 OAPI calls so concurrent requests never clobber COM state
_SAP_LOCK = threading.Lock()


class _SapState:
    """SAP2000 instance this worker started (only that one is ever closed)."""
    owned = False
    last_used = 0.0


_SAP_STATE = _SapState()


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


# ── OAPI helpers ────────────────────────────────────────────────────────────
def _names(ret: Any) -> list[str]:
    """Unpack a (ret, count, names[]) GetNameList tuple."""
    try:
        if ret[0] == 0 and len(ret) >= 3 and ret[2]:
            return [str(n) for n in ret[2]]
    except Exception:
        pass
    return []


def _step_key(case: str, step: Any) -> str:
    """Envelope combos return Max and Min rows; keep them apart."""
    st = str(step or "").strip()
    return f"{case} {st}" if st.lower() in ("max", "min") else str(case)


def _base_case(key: str) -> str:
    for suf in (" Max", " Min", " max", " min"):
        if key.endswith(suf):
            return key[: -len(suf)]
    return key


def _case_type(model: Any, sap: Any, name: str) -> Any:
    """OAPI load case type (eLoadCaseType), or None if it can't be read."""
    try:
        r = model.LoadCases.GetTypeOAPI(name, sap.eLoadCaseType.LinearStatic, 0)
        if r[0] == 0:
            return r[1]
    except Exception:
        pass
    return None


def _select_for_output(model: Any, name: str, combos: list[str]) -> None:
    model.Results.Setup.DeselectAllCasesAndCombosForOutput()
    if name in combos:
        model.Results.Setup.SetComboSelectedForOutput(name)
    else:
        model.Results.Setup.SetCaseSelectedForOutput(name)


def _program_version(model: Any) -> str:
    try:
        r = model.GetVersion("", 0.0)
        if r[0] == 0 and r[1]:
            return str(r[1])
    except Exception:
        pass
    return "25"


def _design_code(model: Any) -> str:
    try:
        r = model.DesignSteel.GetCode("")
        if r[0] == 0:
            return str(r[1])
    except Exception:
        pass
    return ""


def _read_geometry(model: Any, frame_names: list[str], frame_sections: dict[str, str]) -> dict[str, Any]:
    joints: dict[str, list[float]] = {}
    restraints: dict[str, list[bool]] = {}
    for pname in _names(model.PointObj.GetNameList(0, [])):
        try:
            r = model.PointObj.GetCoordCartesian(pname, 0.0, 0.0, 0.0, "Global")
            if r[0] == 0:
                joints[pname] = [float(r[1]), float(r[2]), float(r[3])]
        except Exception:
            continue
        try:
            rr = model.PointObj.GetRestraint(pname, [False] * 6)
            if rr[0] == 0:
                vals = [bool(v) for v in rr[1]]
                if any(vals):
                    restraints[pname] = vals
        except Exception:
            pass
    frames: list[dict[str, Any]] = []
    for fname in frame_names:
        try:
            r = model.FrameObj.GetPoints(fname, "", "")
            if r[0] != 0:
                continue
            angle = 0.0
            try:
                la = model.FrameObj.GetLocalAxes(fname, 0.0, False)
                if la[0] == 0:
                    angle = float(la[1])
            except Exception:
                pass
            frames.append({
                "name": fname,
                "i": str(r[1]),
                "j": str(r[2]),
                "section": frame_sections.get(fname, ""),
                "angle": angle,
            })
        except Exception:
            continue
    areas: list[dict[str, Any]] = []
    try:
        for aname in _names(model.AreaObj.GetNameList(0, [])):
            try:
                r = model.AreaObj.GetPoints(aname, 0, [])
                if r[0] == 0 and int(r[1]) >= 3:
                    areas.append({"name": aname, "points": [str(p) for p in r[2]]})
            except Exception:
                continue
    except Exception:
        pass
    interior = sap2000_post.interior_joints(
        {k: tuple(v) for k, v in joints.items()}, frames,
    )
    for f in frames:
        if f["name"] in interior:
            f["interior"] = interior[f["name"]]
    return {"joints": joints, "restraints": restraints, "frames": frames, "areas": areas}


def _isotropic_E(model: Any, mat: str) -> float:
    """Young's modulus (kN/m²). v25 returns E, U, A, G; older builds E, U, A."""
    last: Exception | None = None
    for args in ((0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0)):
        try:
            r = model.PropMaterial.GetMPIsotropic(mat, *args)
            if r[0] == 0 and float(r[1]) > 0:
                return float(r[1])
        except Exception as e:  # wrong overload → try the other signature
            last = e
    raise RuntimeError(f"GetMPIsotropic({mat}) failed: {last}")


def _read_stiffness(model: Any, frame_names: list[str], frame_sections: dict[str, str]) -> dict[str, tuple[float, float]]:
    """(EI22, EI33) per frame in kN·m², including section and object modifiers."""
    sec_cache: dict[str, tuple[float, float, list[float]]] = {}
    out: dict[str, tuple[float, float]] = {}
    for fname in frame_names:
        sec = frame_sections.get(fname)
        if not sec:
            continue
        if sec not in sec_cache:
            try:
                sp = model.PropFrame.GetSectProps(sec, *([0.0] * 12))
                i22, i33 = float(sp[5]), float(sp[6])
                mat = str(model.PropFrame.GetMaterial(sec, "")[1])
                E = _isotropic_E(model, mat)
                mods = [1.0] * 8
                try:
                    m = model.PropFrame.GetModifiers(sec, [1.0] * 8)
                    if m[0] == 0:
                        mods = [float(v) for v in m[1]]
                except Exception:
                    pass
                sec_cache[sec] = (E * i22, E * i33, mods)
            except Exception as e:
                log.warning(f"Section stiffness for {sec} unavailable: {e}")
                sec_cache[sec] = (0.0, 0.0, [1.0] * 8)
        ei22, ei33, smods = sec_cache[sec]
        omods = [1.0] * 8
        try:
            m = model.FrameObj.GetModifiers(fname, [1.0] * 8)
            if m[0] == 0:
                omods = [float(v) for v in m[1]]
        except Exception:
            pass
        out[fname] = (ei22 * smods[4] * omods[4], ei33 * smods[5] * omods[5])
    return out


def _pick_plot_combo(
    inputs: dict[str, Any], load_cases: list[str], load_combos: list[str], peak_m3: dict[str, float],
) -> str:
    """Combo whose V/M diagrams go in the report: the governing ULS combination."""
    wanted = str(inputs.get("plot_combo") or "").strip()
    if wanted and (wanted in load_combos or wanted in load_cases):
        return wanted
    uls = [c for c in load_combos if not sap2000_post.is_sls_name(c)]
    for pool in (uls, load_combos, load_cases):
        if pool:
            return max(pool, key=lambda c: peak_m3.get(c, 0.0))
    return ""


def _pick_load_pattern(model: Any, inputs: dict[str, Any]) -> str:
    """Load pattern drawn on the 'Frame Span Loads' view (LIVE by preference)."""
    pats = _names(model.LoadPatterns.GetNameList(0, []))
    wanted = str(inputs.get("load_pattern") or "").strip()
    if wanted in pats:
        return wanted
    for p in pats:
        u = p.upper()
        if "LIVE" in u or u in ("LL", "Q", "QK", "IMPOSED"):
            return p
    for p in pats:
        if not any(t in p.upper() for t in ("DEAD", "SDL", "SELF", "SW")):
            return p
    return pats[0] if pats else ""


def _pick_deflection_case(
    inputs: dict[str, Any], load_cases: list[str], load_pattern: str,
    disp_by_case: dict[str, dict[str, tuple[float, float, float]]],
) -> str:
    wanted = str(inputs.get("deflection_case") or "").strip()
    if wanted:
        return wanted
    if load_pattern in load_cases:
        return load_pattern

    def peak(key: str) -> float:
        return max((max(abs(c) for c in d) for d in disp_by_case.get(key, {}).values()), default=0.0)

    pool = [k for k in disp_by_case if _base_case(k) in load_cases]
    if pool:
        return _base_case(max(pool, key=peak))
    return load_cases[0] if load_cases else ""


def _collect_views(
    model: Any, sap: Any, geometry: dict[str, Any], plot_combo: str, deflection_case: str,
    load_pattern: str, load_combos: list[str], steel_design_results: list[dict[str, Any]],
) -> dict[str, Any]:
    views: dict[str, Any] = {
        "joints": geometry["joints"],
        "restraints": geometry["restraints"],
        "frames": geometry["frames"],
        "areas": geometry.get("areas", []),
        "plot_combo": plot_combo,
        "deflection_case": deflection_case,
        "load_pattern": load_pattern,
    }

    # Frame forces along each member for the plotted combo (Max/Min if envelope)
    frame_forces: dict[str, dict[str, Any]] = {}
    reactions: dict[str, list[float]] = {}
    if plot_combo:
        _select_for_output(model, plot_combo, load_combos)
        rf = model.Results.FrameForce(
            "ALL", sap.eItemTypeElm.GroupElm,
            0, [], [], [], [], [], [], [], [], [], [], [], [], []
        )
        n = int(rf[1]) if rf[0] == 0 else 0
        tmp: dict[str, dict[float, dict[str, float]]] = {}
        for i in range(n):
            fname = str(rf[2][i])
            sta = round(float(rf[3][i]), 6)
            rec = tmp.setdefault(fname, {}).setdefault(sta, {})
            for comp, idx in (("P", 9), ("V2", 10), ("V3", 11), ("T", 12), ("M2", 13), ("M3", 14)):
                v = float(rf[idx][i])
                rec[f"{comp}_max"] = max(rec.get(f"{comp}_max", v), v)
                rec[f"{comp}_min"] = min(rec.get(f"{comp}_min", v), v)
        for fname, by_sta in tmp.items():
            stas = sorted(by_sta)
            entry: dict[str, Any] = {"sta": stas}
            for comp in ("P", "V2", "V3", "T", "M2", "M3"):
                for ext in ("max", "min"):
                    entry[f"{comp}_{ext}"] = [round(by_sta[s][f"{comp}_{ext}"], 5) for s in stas]
            frame_forces[fname] = entry

        rr = model.Results.JointReact(
            "ALL", sap.eItemTypeElm.GroupElm,
            0, [], [], [], [], [], [], [], [], [], [], []
        )
        n = int(rr[1]) if rr[0] == 0 else 0
        for i in range(n):
            jn = str(rr[2][i])
            vals = [float(rr[k][i]) for k in range(7, 13)]
            prev = reactions.get(jn)
            if prev is None:
                reactions[jn] = vals
            else:
                reactions[jn] = [v if abs(v) > abs(p) else p for v, p in zip(vals, prev)]
    views["frame_forces"] = frame_forces
    views["reactions"] = {k: [round(v, 4) for v in vals] for k, vals in reactions.items()}

    # Joint displacements + rotations for the deformed shape
    joint_displ: dict[str, list[float]] = {}
    if deflection_case:
        _select_for_output(model, deflection_case, load_combos)
        rd = model.Results.JointDispl(
            "ALL", sap.eItemTypeElm.GroupElm,
            0, [], [], [], [], [], [], [], [], [], [], []
        )
        n = int(rd[1]) if rd[0] == 0 else 0
        for i in range(n):
            jn = str(rd[2][i])
            vals = [float(rd[k][i]) for k in range(7, 13)]
            mag = sum(v * v for v in vals[:3])
            prev = joint_displ.get(jn)
            if prev is None or mag > sum(v * v for v in prev[:3]):
                joint_displ[jn] = vals
    views["joint_displ"] = {k: [float(f"{v:.6g}") for v in vals] for k, vals in joint_displ.items()}

    views["loads_by_pattern"] = _read_loads(model, sap)
    views["loads"] = views["loads_by_pattern"].get(load_pattern, {})

    views["stress_ratios"] = {d["frame"]: d["ratio"] for d in steel_design_results}
    return views


def _read_loads(model: Any, sap: Any) -> dict[str, dict[str, Any]]:
    """Applied loads of every load pattern: frame span/point, joint and area loads."""
    pats = _names(model.LoadPatterns.GetNameList(0, []))
    out: dict[str, dict[str, Any]] = {
        p: {"frame_dist": [], "frame_point": [], "joint": [], "area": [], "self_weight": 0.0} for p in pats
    }

    def bucket(pat: Any) -> dict[str, Any] | None:
        return out.get(str(pat))

    for p in pats:
        try:
            r = model.LoadPatterns.GetSelfWTMultiplier(p, 0.0)
            if r[0] == 0:
                out[p]["self_weight"] = float(r[1])
        except Exception:
            pass
    try:
        r = model.FrameObj.GetLoadDistributed(
            "ALL", 0, [], [], [], [], [], [], [], [], [], [], [], sap.eItemType.Group
        )
        if r[0] == 0:
            for i in range(int(r[1])):
                b = bucket(r[3][i])
                if b is None or int(r[4][i]) != 1:
                    continue
                b["frame_dist"].append({
                    "frame": str(r[2][i]), "csys": str(r[5][i]), "dir": int(r[6][i]),
                    "rd1": float(r[7][i]), "rd2": float(r[8][i]),
                    "val1": float(r[11][i]), "val2": float(r[12][i]),
                })
    except Exception as e:
        log.debug(f"GetLoadDistributed failed: {e}")
    try:
        r = model.FrameObj.GetLoadPoint("ALL", 0, [], [], [], [], [], [], [], [], sap.eItemType.Group)
        if r[0] == 0:
            for i in range(int(r[1])):
                b = bucket(r[3][i])
                if b is None or int(r[4][i]) != 1:
                    continue
                b["frame_point"].append({
                    "frame": str(r[2][i]), "csys": str(r[5][i]), "dir": int(r[6][i]),
                    "rd": float(r[7][i]), "val": float(r[9][i]),
                })
    except Exception as e:
        log.debug(f"GetLoadPoint failed: {e}")
    try:
        r = model.PointObj.GetLoadForce(
            "ALL", 0, [], [], [], [], [], [], [], [], [], [], sap.eItemType.Group
        )
        if r[0] == 0:
            for i in range(int(r[1])):
                b = bucket(r[3][i])
                if b is None:
                    continue
                b["joint"].append({
                    "joint": str(r[2][i]),
                    "F": [float(r[6][i]), float(r[7][i]), float(r[8][i])],
                })
    except Exception as e:
        log.debug(f"GetLoadForce failed: {e}")
    # Area loads: uniform (on the shell) and uniform-to-frame (distributed to frames)
    for kind, getter, nargs in (
        ("uniform", "GetLoadUniform", 5),
        ("to_frame", "GetLoadUniformToFrame", 6),
    ):
        try:
            r = getattr(model.AreaObj, getter)("ALL", 0, *([[]] * nargs), sap.eItemType.Group)
            if r[0] == 0:
                for i in range(int(r[1])):
                    b = bucket(r[3][i])
                    if b is None:
                        continue
                    b["area"].append({
                        "area": str(r[2][i]), "csys": str(r[4][i]), "dir": int(r[5][i]),
                        "val": float(r[6][i]), "kind": kind,
                    })
        except Exception as e:
            log.debug(f"AreaObj.{getter} failed: {e}")
    return out


def _combo_labels(model: Any, sap: Any, load_combos: list[str]) -> dict[str, str]:
    """
    'DSTL2' -> '1.35G+1.5Q'. Combos already named as a formula (containing '+')
    keep their own name.
    """
    symbols: dict[str, str] = {}

    def symbol(case: str) -> str:
        if case not in symbols:
            ptype = None
            try:
                r = model.LoadPatterns.GetLoadType(case, sap.eLoadPatternType.Dead)
                if r[0] == 0:
                    ptype = r[1]
            except Exception:
                pass
            symbols[case] = sap2000_post.case_symbol(case, ptype)
        return symbols[case]

    def terms(combo: str, depth: int = 0) -> tuple[list[tuple[str, float]], bool]:
        r = model.RespCombo.GetCaseList(combo, 0, [], [], [])
        if r[0] != 0:
            return [], False
        envelope = False
        try:
            t = model.RespCombo.GetTypeOAPI(combo, 0)
            envelope = t[0] == 0 and int(t[1]) == 1
        except Exception:
            pass
        out: list[tuple[str, float]] = []
        for k in range(int(r[1])):
            name, sf = str(r[3][k]), float(r[4][k])
            is_combo = "combo" in str(r[2][k]).lower() or str(r[2][k]) == "1"
            if is_combo and depth < 3 and not envelope:
                sub, _ = terms(name, depth + 1)
                out += [(n, f * sf) for n, f in sub]
            else:
                out.append((name, sf))
        return out, envelope

    labels: dict[str, str] = {}
    for c in load_combos:
        if "+" in c:
            continue
        try:
            t, env = terms(c)
            for name, _ in t:
                symbol(name)
            txt = sap2000_post.combo_formula(t, symbols, envelope=env)
            if txt:
                labels[c] = txt
        except Exception as e:
            log.debug(f"Combo formula for {c} unavailable: {e}")
    return labels


def _apply_combo_labels(res: dict[str, Any], labels: dict[str, str]) -> None:
    """Show combos as their factored formula everywhere in the result."""
    if not labels:
        return

    def lab(x: Any) -> Any:
        return labels.get(x, x) if isinstance(x, str) else x

    for env in res.get("member_envelopes", []):
        env["gov_case"] = lab(env.get("gov_case"))
    for row in res.get("support_reactions", []) + res.get("joint_displacements", []):
        row["case"] = lab(row.get("case"))
    for d in res.get("member_deflections", []):
        d["case"] = lab(d.get("case"))
    for sr in res.get("steel_design_results", []):
        combo = sr.get("combo", "")
        for raw, txt in labels.items():
            if combo == raw or combo.startswith(raw + " "):
                sr["combo"] = txt + combo[len(raw):]
                break
    for c in res.get("checks", []):
        for raw, txt in labels.items():
            c["name"] = re.sub(rf"(?<![\w]){re.escape(raw)}(?![\w])", txt, c["name"])
    inp = res.get("inputs", {})
    if inp.get("load_combos") and inp["load_combos"] != "None":
        seen: list[str] = []
        for c in inp["load_combos"].split(", "):
            t = lab(c)
            if t not in seen:
                seen.append(t)
        inp["load_combos"] = ", ".join(seen)
    inp["plot_combo"] = lab(inp.get("plot_combo"))
    inp["deflection_case"] = lab(inp.get("deflection_case"))
    v = res.get("views") or {}
    if v:
        v["plot_combo_label"] = lab(v.get("plot_combo"))
        v["deflection_case_label"] = lab(v.get("deflection_case"))
        v["combo_labels"] = labels


def _skip_unneeded_eigen_cases(model: Any, sap: Any) -> list[str]:
    """
    Don't solve Modal / Buckling cases when nothing depends on them: their
    results are discarded anyway (mode shapes, not load effects), and an
    eigen solution is often the slowest part of a run. Kept whenever a
    response-spectrum or modal time-history case needs the modes.
    """
    try:
        cases = _names(model.LoadCases.GetNameList(0, []))
        types = {c: _case_type(model, sap, c) for c in cases}
        as_int: dict[str, int | None] = {}
        for c, t in types.items():
            try:
                as_int[c] = int(t) if t is not None else None
            except (TypeError, ValueError):
                as_int[c] = None
        if any(t in _NEEDS_MODAL_TYPES for t in as_int.values()):
            return []
        skip = [c for c in cases if sap2000_post.is_non_physical_case(c, types[c])]
        for c in skip:
            model.Analyze.SetRunCaseFlag(c, False, False)
        return skip
    except Exception as e:
        log.debug(f"Could not skip eigen cases: {e}")
        return []


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "y", "yes", "true", "on")
    return bool(v)


def _member_deflections(
    inputs: dict[str, Any], geometry: dict[str, Any], load_cases: list[str], load_combos: list[str],
    disp_by_case: dict[str, dict[str, tuple[float, float, float]]],
    moments_by_case: dict[str, dict[str, dict[str, list[float]]]],
    stiffness: dict[str, tuple[float, float]],
) -> list[dict[str, Any]]:
    wanted = inputs.get("deflection_cases")
    if isinstance(wanted, str):
        wanted = [w.strip() for w in wanted.split(",") if w.strip()]
    if wanted:
        bases = set(wanted)
    else:
        bases = set(load_cases) | {c for c in load_combos if sap2000_post.is_sls_name(c)}
    sets = {k: v for k, v in disp_by_case.items() if _base_case(k) in bases}
    joints = {k: tuple(v) for k, v in geometry["joints"].items()}
    frames = geometry["frames"]
    interior = {f["name"]: f["interior"] for f in frames if f.get("interior")}
    chains = sap2000_post.build_member_chains(
        joints, frames, geometry["restraints"].keys(), interior=interior,
    )
    return sap2000_post.chain_deflection_checks(
        chains, joints, {f["name"]: f for f in frames}, sets,
        {k: v for k, v in moments_by_case.items() if k in sets},
        stiffness,
        span_ratio=float(inputs.get("deflection_span_ratio") or 200.0),
        cantilever_ratio=float(inputs.get("cantilever_span_ratio") or 180.0),
    )


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

    timings: dict[str, float] = {}
    t_start = t_last = time.monotonic()

    def stamp(name: str) -> None:
        nonlocal t_last
        now = time.monotonic()
        timings[name] = round(now - t_last, 2)
        t_last = now

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
                _SAP_STATE.owned = False
                sap_obj = helper_iface.CreateObjectProgID("CSI.SAP2000.API.SapObject")
                ret = sap_obj.ApplicationStart(sap.eUnits.kN_m_C, visible)
                if ret != 0:
                    raise RuntimeError(f"SAP2000 ApplicationStart failed with code {ret}")
                _SAP_STATE.owned = True
            stamp("start_sap2000" if not attached else "attach_sap2000")

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

                # Model geometry for the SAP2000-style report views
                geometry = _read_geometry(model, frame_names, frame_sections)

                # Denser output stations give smooth V/M diagrams and an accurate
                # member-sag integration (analysis runs on a temp copy of the model).
                n_sta = int(inputs.get("output_stations", 11) or 0)
                if n_sta > 0:
                    try:
                        model.SetModelIsLocked(False)
                    except Exception:
                        pass
                    for fname in frame_names:
                        try:
                            model.FrameObj.SetOutputStations(fname, 2, 0.0, n_sta, False, False)
                        except Exception:
                            break

                skipped_cases = _skip_unneeded_eigen_cases(model, sap)
                stamp("open_model")

                # Run structural analysis
                ret = model.Analyze.RunAnalysis()
                if ret != 0:
                    raise RuntimeError(f"SAP2000 Analyze.RunAnalysis() failed with code {ret}")
                stamp("analysis")

                # Discover load cases and load combinations
                ret_lcases = model.LoadCases.GetNameList(0, [])
                all_lcases = list(ret_lcases[2]) if (len(ret_lcases) >= 3 and ret_lcases[2]) else []

                # CRITICAL: drop non-physical eigen cases (Modal / Buckling). Their results
                # are mass-normalised mode shapes, not responses to load, and produce
                # pseudo-forces and displacements in the millions. Classified by OAPI case
                # type, so a modal case named e.g. "MODES" or "EQ-Ritz" is also excluded.
                load_cases = [
                    lc for lc in all_lcases
                    if not sap2000_post.is_non_physical_case(lc, _case_type(model, sap, lc))
                ]
                excluded_cases = [lc for lc in all_lcases if lc not in load_cases]

                ret_combos = model.RespCombo.GetNameList(0, [])
                load_combos = list(ret_combos[2]) if (len(ret_combos) >= 3 and ret_combos[2]) else []

                # Select only real physical load cases and combinations for output extraction
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
                disp_by_case: dict[str, dict[str, tuple[float, float, float]]] = {}
                for i in range(n_displ):
                    j_name = str(res_displ[2][i])
                    lc_name = str(res_displ[4][i])
                    u1_mm = float(res_displ[7][i]) * 1000.0
                    u2_mm = float(res_displ[8][i]) * 1000.0
                    u3_mm = float(res_displ[9][i]) * 1000.0
                    mag_mm = max(abs(u1_mm), abs(u2_mm), abs(u3_mm))
                    disp_by_case.setdefault(_step_key(lc_name, res_displ[5][i]), {})[j_name] = (
                        u1_mm / 1000.0, u2_mm / 1000.0, u3_mm / 1000.0,
                    )

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
                moments_by_case: dict[str, dict[str, dict[str, list[float]]]] = {}
                lc_peak_m3: dict[str, float] = {}
                for i in range(n_frame):
                    f_name = str(res_frame[2][i])
                    lc_name = str(res_frame[6][i])
                    p = float(res_frame[9][i])
                    v2 = float(res_frame[10][i])
                    v3 = float(res_frame[11][i])
                    t = float(res_frame[12][i])
                    m2 = float(res_frame[13][i])
                    m3 = float(res_frame[14][i])

                    key = _step_key(lc_name, res_frame[7][i])
                    mom = moments_by_case.setdefault(key, {}).setdefault(
                        f_name, {"sta": [], "M2": [], "M3": []}
                    )
                    mom["sta"].append(float(res_frame[3][i]))
                    mom["M2"].append(m2)
                    mom["M3"].append(m3)
                    lc_peak_m3[lc_name] = max(lc_peak_m3.get(lc_name, 0.0), abs(m3))

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

                stamp("results")

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

                stamp("steel_design")

                # ── 5. Data for the SAP2000-style views + member deflection ────
                try:
                    combo_labels = _combo_labels(model, sap, load_combos)
                except Exception as e:
                    log.debug(f"Combo formulas unavailable: {e}")
                    combo_labels = {}
                plot_combo = _pick_plot_combo(inputs, load_cases, load_combos, lc_peak_m3)
                load_pattern = _pick_load_pattern(model, inputs)
                deflection_case = _pick_deflection_case(inputs, load_cases, load_pattern, disp_by_case)
                stiffness = _read_stiffness(model, frame_names, frame_sections)
                views: dict[str, Any] = {}
                if inputs.get("views", True):
                    try:
                        views = _collect_views(
                            model, sap, geometry, plot_combo, deflection_case, load_pattern,
                            load_combos, steel_design_results,
                        )
                    except Exception as e:  # views must never break the analysis
                        log.warning(f"SAP2000 view data collection failed: {e}")
                        views = {"error": str(e)}
                    dm = moments_by_case.get(deflection_case) or {}
                    views["deflection_moments"] = {
                        f: {k: [round(x, 6) for x in v] for k, v in d.items()} for f, d in dm.items()
                    }
                    views["stiffness"] = {f: [round(a, 3), round(b, 3)] for f, (a, b) in stiffness.items()}
                    if inputs.get("view_plan") is not None or inputs.get("view_elev") is not None:
                        views["view"] = {
                            "plan": float(inputs.get("view_plan", 315.0)),
                            "elev": float(inputs.get("view_elev", 35.0)),
                        }
                    views.update({
                        "program": "SAP2000",
                        "version": _program_version(model),
                        "model_file": src_path.name,
                        "units": "KN, m, C",
                    })
                    if steel_design_results:
                        views["design_code"] = sap2000_post.friendly_design_code(_design_code(model))

                stamp("views")
            finally:
                # Kept open: app_keeper closes our instance once it has been idle
                # long enough outside working hours. Someone else's instance is
                # never closed — only cleared of the job model.
                if not keep_open and (not attached or _SAP_STATE.owned):
                    try:
                        sap_obj.ApplicationExit(False)
                    except Exception:
                        pass
                    _SAP_STATE.owned = False
                else:
                    try:
                        model.File.NewBlank()
                    except Exception:
                        pass
                _SAP_STATE.last_used = time.monotonic()
    finally:
        _uninit_com_thread()
    stamp("close")
    timings["total"] = round(time.monotonic() - t_start, 2)
    log.info(f"SAP2000 run {src_path.name}: {timings}")

    # ── 5. Build Engineering Checks ─────────────────────────────────────────
    explicit_disp_limit = bool(inputs.get("deflection_limit_mm"))
    # Deflection output is opt-in: caption `deflection=yes` (or a numeric limit).
    deflection_on = _truthy(inputs.get("deflection")) or explicit_disp_limit
    disp_limit_mm = float(inputs.get("deflection_limit_mm") or 0.0)
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

    # Deflection. Default: every physical span relative to its supports, span/200
    # (span/180 cantilever) under unfactored cases + SLS combos. An explicit
    # `deflection_limit_mm` switches to the old absolute max-nodal check.
    member_deflections: list[dict[str, Any]] = []
    if not deflection_on:
        pass
    elif explicit_disp_limit:
        disp_util = round(max_disp_mm / disp_limit_mm, 3) if disp_limit_mm > 0 else 0.0
        checks.append({
            "name": f"Max Nodal Deflection (Joint {gov_disp_joint}, Case {gov_disp_case})",
            "demand": f"{max_disp_mm:.2f} mm",
            "capacity": f"{disp_limit_mm:.2f} mm",
            "util": disp_util,
            "pass": max_disp_mm <= disp_limit_mm,
        })
    elif geometry.get("frames"):
        member_deflections = _member_deflections(
            inputs, geometry, load_cases, load_combos, disp_by_case, moments_by_case, stiffness,
        )
        if member_deflections:
            gov = member_deflections[0]
            label = "Cantilever" if gov["cantilever"] else "Span"
            fr = gov["frames"]
            fr_txt = fr[0] if len(fr) == 1 else f"{fr[0]}…{fr[-1]}"
            checks.append({
                "name": (
                    f"Member Deflection ({label} L={gov['span_m'] * 1000:.0f} mm, "
                    f"Frame {fr_txt}, Case {gov['case']})"
                ),
                "demand": f"{gov['delta_mm']:.2f} mm",
                "capacity": f"L/{gov['ratio']:.0f} = {gov['limit_mm']:.2f} mm",
                "util": gov["util"],
                "pass": gov["util"] <= 1.0,
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
        "deflection_limit_mm": disp_limit_mm if explicit_disp_limit else None,
        "deflection_checked": deflection_on,
        "deflection_criteria": (
            "Not checked" if not deflection_on else
            f"Absolute nodal ≤ {disp_limit_mm:g} mm" if explicit_disp_limit else
            f"Span/{float(inputs.get('deflection_span_ratio') or 200):g}, "
            f"cantilever/{float(inputs.get('cantilever_span_ratio') or 180):g} (relative to supports)"
        ),
        "excluded_cases": ", ".join(excluded_cases) if excluded_cases else "None",
        "plot_combo": plot_combo,
        "deflection_case": deflection_case,
    }

    if not deflection_on and views:
        for k in ("joint_displ", "deflection_moments", "stiffness"):
            views.pop(k, None)
        views["deflection_case"] = ""

    res = {
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
        "joint_displacements": joint_displacements if deflection_on else [],
        "steel_design_results": steel_design_results[:20],
        "member_deflections": member_deflections[:20],
        "views": views,
        "timings": timings,
        "skipped_cases": skipped_cases,
    }
    _apply_combo_labels(res, combo_labels)
    return res


def reap_idle_sap2000(now: float | None = None) -> bool:
    """Close the worker-started SAP2000 when idle past the limit (off hours only)."""
    limit = app_keeper.sap2000_idle_limit_s()
    if not _SAP_STATE.owned or limit is None:
        return False
    if app_keeper.idle_for(_SAP_STATE.last_used, now) < limit:
        return False
    if not _SAP_LOCK.acquire(blocking=False):  # a job is running
        return False
    try:
        _init_com_thread()
        try:
            sap = _load_sap_module()
            sap_obj = sap.cHelper(sap.Helper()).GetObject("CSI.SAP2000.API.SapObject")
            if sap_obj is not None:
                sap_obj.ApplicationExit(False)
                log.info("Closed idle SAP2000 (outside working hours)")
        finally:
            _uninit_com_thread()
        _SAP_STATE.owned = False
        return True
    finally:
        _SAP_LOCK.release()


def prewarm_sap2000(visible: bool = True) -> bool:
    """Start SAP2000 during working hours so the day's first job does not wait for it."""
    if not (app_keeper.sap2000_prewarm_enabled() and app_keeper.in_work_hours()):
        return False
    if _SAP_STATE.owned or not is_sap2000_available():
        return False
    if not _SAP_LOCK.acquire(blocking=False):
        return False
    try:
        _init_com_thread()
        try:
            sap = _load_sap_module()
            helper_iface = sap.cHelper(sap.Helper())
            if helper_iface.GetObject("CSI.SAP2000.API.SapObject") is not None:
                return False  # already running (someone's session or a previous worker)
            sap_obj = helper_iface.CreateObjectProgID("CSI.SAP2000.API.SapObject")
            if sap_obj.ApplicationStart(sap.eUnits.kN_m_C, visible) != 0:
                return False
            _SAP_STATE.owned = True
            _SAP_STATE.last_used = time.monotonic()
            log.info("Pre-started SAP2000 for working hours")
            return True
        finally:
            _uninit_com_thread()
    finally:
        _SAP_LOCK.release()


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
