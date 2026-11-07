"""
engines/sap2000_post.py — Pure-Python post-processing for SAP2000 OAPI results
=============================================================================
No SAP2000 / pythonnet dependency, so everything here is unit-testable on Linux.

  - `is_non_physical_case()`   : MODAL / BUCKLING filter by OAPI case type (name fallback)
  - `is_sls_name()`            : serviceability combo name heuristic
  - `frame_local_axes()`       : SAP2000 default frame local axes (1, 2, 3) incl. angle
  - `bending_deflection()`     : member sag relative to its chord, from the M diagram
  - `build_member_chains()`    : join collinear frame objects into physical spans
  - `chain_deflection_checks()`: span/200 (span/180 cantilever) relative deflection check
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable

Vec = tuple[float, float, float]

# eLoadCaseType integer values (CSI OAPI): 3 = Modal, 10 = Buckling
_NON_PHYSICAL_TYPE_INTS = {3, 10}
_NON_PHYSICAL_TYPE_NAMES = ("MODAL", "BUCKLING")
_NON_PHYSICAL_NAME_TOKENS = ("MODAL", "MODE", "BUCKL", "EIGEN", "RITZ")

_SLS_RE = re.compile(r"(SLS|SERV|CHAR|DEFL|QP|FREQ|QUASI)", re.I)


def is_non_physical_case(name: str, case_type: Any = None) -> bool:
    """True for eigen-type cases (Modal / Buckling) whose results are mode shapes, not loads."""
    if case_type is not None:
        try:
            if int(case_type) in _NON_PHYSICAL_TYPE_INTS:
                return True
            return False
        except (TypeError, ValueError):
            text = str(case_type).upper()
            if any(t in text for t in _NON_PHYSICAL_TYPE_NAMES):
                return True
            if text:
                return False
    upper = str(name).upper()
    return any(tok in upper for tok in _NON_PHYSICAL_NAME_TOKENS)


def is_sls_name(name: str) -> bool:
    return bool(_SLS_RE.search(str(name)))


# ── Vector helpers ──────────────────────────────────────────────────────────
def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _mul(a: Vec, k: float) -> Vec:
    return (a[0] * k, a[1] * k, a[2] * k)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a: Vec) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a: Vec) -> Vec:
    n = _norm(a)
    return (0.0, 0.0, 0.0) if n == 0 else _mul(a, 1.0 / n)


def frame_local_axes(pi: Vec, pj: Vec, angle_deg: float = 0.0) -> tuple[Vec, Vec, Vec]:
    """
    SAP2000 default frame local axes.
    Local 1 runs I→J. For a vertical member local 2 is global +X; otherwise local 2
    is the projection of global +Z perpendicular to local 1 (points "up").
    Local 3 = 1 × 2. The local-axes angle rotates 2 and 3 about 1.
    """
    e1 = _unit(_sub(pj, pi))
    if abs(e1[2]) > 0.999:  # vertical (SAP tolerance ~ sin of 0.057°)
        e2 = (1.0, 0.0, 0.0)
        e2 = _unit(_sub(e2, _mul(e1, _dot(e2, e1))))
    else:
        z = (0.0, 0.0, 1.0)
        e2 = _unit(_sub(z, _mul(e1, _dot(z, e1))))
    e3 = _cross(e1, e2)
    if angle_deg:
        a = math.radians(angle_deg)
        c, s = math.cos(a), math.sin(a)
        e2, e3 = _add(_mul(e2, c), _mul(e3, s)), _add(_mul(e2, -s), _mul(e3, c))
    return e1, e2, e3


def bending_deflection(stations: list[float], moments: list[float], EI: float) -> list[float]:
    """
    Deflection relative to the member chord from its bending-moment diagram
    (EI·v'' = +M: positive / sagging M3 bends the member down along -2;
    v(0) = v(L) = 0), by double trapezoid integration.
    Returns v at each station (same units as stations when M in kN·m, EI in kN·m²).
    """
    n = len(stations)
    if n < 2 or EI <= 0:
        return [0.0] * n
    pairs = sorted(zip(stations, moments))
    s = [p[0] for p in pairs]
    k = [p[1] / EI for p in pairs]
    theta = [0.0]
    for i in range(1, n):
        theta.append(theta[-1] + 0.5 * (k[i] + k[i - 1]) * (s[i] - s[i - 1]))
    v = [0.0]
    for i in range(1, n):
        v.append(v[-1] + 0.5 * (theta[i] + theta[i - 1]) * (s[i] - s[i - 1]))
    L = s[-1] - s[0]
    if L <= 0:
        return [0.0] * n
    return [v[i] - (s[i] - s[0]) / L * v[-1] for i in range(n)]


# ── Member chains (physical spans made of several collinear frame objects) ──
def build_member_chains(
    joints: dict[str, Vec],
    frames: list[dict[str, Any]],
    restrained: Iterable[str],
    collinear_tol_deg: float = 1.0,
    interior: dict[str, list] | None = None,
) -> list[dict[str, Any]]:
    """
    Group collinear frame objects meeting end-to-end into one physical span. A chain
    stops at a restrained joint, a joint with no collinear continuation, or a joint
    where more than one collinear continuation exists.

    Returns [{"frames": [...], "joints": [j0, j1, ...], "length": L, "cantilever": bool}]
    """
    restrained = set(restrained)
    by_name = {f["name"]: f for f in frames if f.get("i") in joints and f.get("j") in joints}
    at_joint: dict[str, list[str]] = {}
    for f in by_name.values():
        at_joint.setdefault(f["i"], []).append(f["name"])
        at_joint.setdefault(f["j"], []).append(f["name"])

    # Joints sitting part-way along another frame (SAP2000 auto-meshes the frame
    # there) are connected to it, so a bar ending on one is not a free end.
    hosted: dict[str, int] = {}
    for fname, items in (interior or {}).items():
        for item in items:
            hosted[str(item[0])] = hosted.get(str(item[0]), 0) + 1

    cos_tol = math.cos(math.radians(collinear_tol_deg))

    def direction(fname: str, from_joint: str) -> Vec:
        f = by_name[fname]
        other = f["j"] if f["i"] == from_joint else f["i"]
        return _unit(_sub(joints[other], joints[from_joint]))

    def other_end(fname: str, joint: str) -> str:
        f = by_name[fname]
        return f["j"] if f["i"] == joint else f["i"]

    def continuation(fname: str, joint: str) -> str | None:
        if joint in restrained:
            return None
        incoming = _mul(direction(fname, joint), -1.0)  # direction travelling into joint
        cands = [
            g for g in at_joint.get(joint, [])
            if g != fname and _dot(direction(g, joint), incoming) >= cos_tol
        ]
        return cands[0] if len(cands) == 1 else None

    visited: set[str] = set()
    chains: list[dict[str, Any]] = []
    for fname in by_name:
        if fname in visited:
            continue
        f = by_name[fname]
        # walk backwards from I end to find the chain start
        start_frame, start_joint = fname, f["i"]
        seen = {fname}
        while True:
            prev = continuation(start_frame, start_joint)
            if prev is None or prev in seen:
                break
            seen.add(prev)
            start_joint = other_end(prev, start_joint)
            start_frame = prev
        # walk forwards collecting frames
        chain_frames = [start_frame]
        chain_joints = [start_joint, other_end(start_frame, start_joint)]
        visited.add(start_frame)
        cur = start_frame
        while True:
            nxt = continuation(cur, chain_joints[-1])
            if nxt is None or nxt in visited:
                break
            visited.add(nxt)
            chain_frames.append(nxt)
            chain_joints.append(other_end(nxt, chain_joints[-1]))
            cur = nxt
        length = _norm(_sub(joints[chain_joints[-1]], joints[chain_joints[0]]))

        def is_free(j: str) -> bool:
            return j not in restrained and len(at_joint.get(j, [])) == 1 and not hosted.get(j)

        cantilever = is_free(chain_joints[0]) != is_free(chain_joints[-1])
        if cantilever and is_free(chain_joints[0]):
            chain_frames.reverse()
            chain_joints.reverse()
        chains.append({
            "frames": chain_frames,
            "joints": chain_joints,
            "length": length,
            "cantilever": cantilever,
        })
    return chains


def chain_deflection_checks(
    chains: list[dict[str, Any]],
    joints: dict[str, Vec],
    frames_by_name: dict[str, dict[str, Any]],
    joint_disp: dict[str, dict[str, Vec]],
    frame_moments: dict[str, dict[str, dict[str, list[float]]]],
    stiffness: dict[str, tuple[float, float]],
    span_ratio: float = 200.0,
    cantilever_ratio: float = 180.0,
) -> list[dict[str, Any]]:
    """
    Relative deflection of every physical span, for every serviceability case.

    joint_disp[case][joint]          -> (U1, U2, U3) in m (global)
    frame_moments[case][frame]       -> {"sta": [...], "M2": [...], "M3": [...]}
    stiffness[frame]                 -> (EI22, EI33) kN·m²

    Simple/continuous spans: deflection relative to the chord joining the span's end
    joints (removes support settlement and rigid-body movement), plus each frame's own
    sag from its moment diagram. Cantilevers: tip displacement relative to the root
    joint (includes root rotation), transverse to the member.
    """
    results: list[dict[str, Any]] = []
    for ch in chains:
        L = ch["length"]
        if L <= 1e-9:
            continue
        j0, jn = ch["joints"][0], ch["joints"][-1]
        e = _unit(_sub(joints[jn], joints[j0]))
        ratio = cantilever_ratio if ch["cantilever"] else span_ratio
        limit_m = L / ratio
        best: dict[str, Any] | None = None
        for case, disp in joint_disp.items():
            if j0 not in disp or jn not in disp:
                continue
            d0, dn = disp[j0], disp[jn]
            worst = 0.0
            if ch["cantilever"]:
                rel = _sub(dn, d0)
                rel = _sub(rel, _mul(e, _dot(rel, e)))
                worst = _norm(rel)
            else:
                pos = 0.0
                for idx, fname in enumerate(ch["frames"]):
                    ja, jb = ch["joints"][idx], ch["joints"][idx + 1]
                    f = frames_by_name[fname]
                    la = _norm(_sub(joints[jb], joints[ja]))
                    da = disp.get(ja, (0.0, 0.0, 0.0))
                    db = disp.get(jb, (0.0, 0.0, 0.0))
                    _, e2, e3 = frame_local_axes(joints[f["i"]], joints[f["j"]], f.get("angle", 0.0))
                    mom = frame_moments.get(case, {}).get(fname)
                    ei22, ei33 = stiffness.get(fname, (0.0, 0.0))
                    if mom and mom.get("sta"):
                        sta = list(mom["sta"])
                        v2 = bending_deflection(sta, list(mom.get("M3", [0.0] * len(sta))), ei33)
                        v3 = bending_deflection(sta, list(mom.get("M2", [0.0] * len(sta))), ei22)
                        sta_sorted = sorted(sta)
                    else:
                        sta_sorted, v2, v3 = [0.0, la], [0.0, 0.0], [0.0, 0.0]
                    flen = sta_sorted[-1] if sta_sorted[-1] > 0 else la
                    forward = f["i"] == ja
                    for k, s in enumerate(sta_sorted):
                        t = s / flen if flen > 0 else 0.0
                        t_chain = t if forward else 1.0 - t
                        d = _add(_mul(da, 1.0 - t_chain), _mul(db, t_chain))
                        x = (pos + t_chain * la) / L
                        chord = _add(_mul(d0, 1.0 - x), _mul(dn, x))
                        rel = _sub(d, chord)
                        rel = _sub(rel, _mul(e, _dot(rel, e)))
                        r2 = _dot(rel, e2) + v2[k]
                        r3a = _dot(rel, e3) + v3[k]
                        r3b = _dot(rel, e3) - v3[k]
                        mag = max(math.hypot(r2, r3a), math.hypot(r2, r3b))
                        worst = max(worst, mag)
                    pos += la
            if best is None or worst > best["delta_m"]:
                best = {"case": case, "delta_m": worst}
        if best is None:
            continue
        results.append({
            "frames": ch["frames"],
            "joints": [j0, jn],
            "span_m": round(L, 4),
            "cantilever": ch["cantilever"],
            "ratio": ratio,
            "case": best["case"],
            "delta_mm": round(best["delta_m"] * 1000.0, 3),
            "limit_mm": round(limit_m * 1000.0, 3),
            "util": round(best["delta_m"] / limit_m, 3) if limit_m > 0 else 0.0,
        })
    results.sort(key=lambda r: r["util"], reverse=True)
    return results


def friendly_design_code(code: str) -> str:
    """Map SAP2000's design code id to the label it prints on stress-ratio plots."""
    c = str(code or "")
    if "Eurocode 3" in c:
        return "EN 1993-1-1:2005/A1:2014"
    return c


def interior_joints(
    joints: dict[str, Vec], frames: list[dict[str, Any]], tol_m: float = 1e-3, max_pairs: int = 5_000_000,
) -> dict[str, list[list[Any]]]:
    """
    Joints lying part-way along a frame object, as [joint, t] with 0 < t < 1 sorted
    by t. SAP2000 connects these to the frame when "auto mesh at intersections" is
    on (the default), so they move with it and carry load into it.
    """
    out: dict[str, list[list[Any]]] = {}
    if len(joints) * len(frames) > max_pairs:
        return out
    items = list(joints.items())
    for f in frames:
        a, b = joints.get(f["i"]), joints.get(f["j"])
        if a is None or b is None:
            continue
        ab = _sub(b, a)
        L2 = _dot(ab, ab)
        if L2 <= 0:
            continue
        L = math.sqrt(L2)
        lo = [min(a[k], b[k]) - tol_m for k in range(3)]
        hi = [max(a[k], b[k]) + tol_m for k in range(3)]
        hits = []
        for jn, p in items:
            if jn in (f["i"], f["j"]):
                continue
            if not all(lo[k] <= p[k] <= hi[k] for k in range(3)):
                continue
            t = _dot(_sub(p, a), ab) / L2
            if t * L <= tol_m or (1 - t) * L <= tol_m:
                continue
            off = _sub(p, _add(a, _mul(ab, t)))
            if _norm(off) <= tol_m:
                hits.append([jn, round(t, 6)])
        if hits:
            out[f["name"]] = sorted(hits, key=lambda h: h[1])
    return out


_SYMBOLS_BY_TYPE = {1: "G", 2: "G", 3: "Q", 4: "Q", 5: "E", 6: "W", 7: "S", 10: "T", 11: "Q"}


def case_symbol(name: str, pattern_type: Any = None) -> str:
    """Eurocode action symbol for a load case: G permanent, Q imposed, W wind…"""
    try:
        if pattern_type is not None and int(pattern_type) in _SYMBOLS_BY_TYPE:
            return _SYMBOLS_BY_TYPE[int(pattern_type)]
    except (TypeError, ValueError):
        pass
    u = str(name).upper()
    if u in ("G", "GK") or any(t in u for t in ("DEAD", "SDL", "SELF", "PERM")):
        return "G"
    if u in ("Q", "QK", "LL") or any(t in u for t in ("LIVE", "IMPOSED")):
        return "Q"
    if "WIND" in u:
        return "W"
    if "SNOW" in u:
        return "S"
    if "TEMP" in u:
        return "T"
    return str(name)


def combo_formula(terms: list[tuple[str, float]], symbols: dict[str, str], envelope: bool = False) -> str:
    """[(case, 1.35), ("LIVE", 1.5)] -> '1.35G+1.5Q' (cases sharing a symbol keep their names)."""
    used: dict[str, int] = {}
    for case, _ in terms:
        sym = symbols.get(case, case)
        used[sym] = used.get(sym, 0) + 1
    parts = []
    for case, sf in terms:
        sym = symbols.get(case, case)
        label = sym if used[sym] == 1 else case
        mag = f"{abs(sf):g}"
        coef = "" if mag == "1" else mag
        sign = "-" if sf < 0 else "+"
        parts.append((sign, f"{coef}{label}"))
    if not parts:
        return ""
    if envelope:
        return "Envelope(" + ", ".join((s if s == "-" else "") + t for s, t in parts) + ")"
    txt = "".join(f"{s}{t}" for s, t in parts)
    return txt[1:] if txt.startswith("+") else txt
