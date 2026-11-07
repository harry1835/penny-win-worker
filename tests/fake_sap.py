"""
tests/fake_sap.py — Minimal stand-in for the SAP2000v1 OAPI (pythonnet) used by tests.

Models a small flat grating: two edge bars along X (y = 0 and y = 1 m) split at
every cross-bar joint, cross (bearing) bars along Y, pinned at the four corners and
the edge mid-points. Results are synthetic but shaped exactly like OAPI tuples:
(ret, count, arr1, arr2, ...). A MODAL case returns huge "mode shape" values so the
tests can prove it never reaches the envelopes.
"""
from __future__ import annotations

import math
import types

NX = 5           # joints along each edge (x = 0 .. 2 m)
LX, LY = 2.0, 1.0
EI = 2.1e8 * (0.012 * 0.09 ** 3 / 12.0)   # 90x12 flat bar, kN·m²
W_LIVE = 1.0     # kN/m on bearing bars
N_STA = 11


class _Enum:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def make_sap_module():
    sap = types.SimpleNamespace()
    sap.eUnits = _Enum(kN_m_C=6)
    sap.eItemTypeElm = _Enum(GroupElm=2, ObjectElm=0)
    sap.eItemType = _Enum(Group=1, Objects=0)
    sap.eLoadCaseType = _Enum(LinearStatic=1, Modal=3)
    model = FakeModel()
    sap_obj = types.SimpleNamespace(SapModel=model, ApplicationStart=lambda *a: 0, ApplicationExit=lambda *a: 0)

    class Helper:
        pass

    class cHelper:
        def __init__(self, _h):
            pass

        def GetObject(self, _prog):
            return sap_obj

        def CreateObjectProgID(self, _prog):
            return sap_obj

    sap.Helper = Helper
    sap.cHelper = cHelper
    sap.model = model
    return sap


def _ns(**kw):
    return types.SimpleNamespace(**kw)


class FakeModel:
    def __init__(self):
        self.joints: dict[str, tuple[float, float, float]] = {}
        self.frames: dict[str, tuple[str, str, str]] = {}
        jid = 1
        grid: dict[tuple[int, int], str] = {}
        for iy, y in enumerate((0.0, LY)):
            for ix in range(NX):
                name = str(jid)
                self.joints[name] = (ix * LX / (NX - 1), y, 0.0)
                grid[(ix, iy)] = name
                jid += 1
        fid = 1
        for iy in range(2):
            for ix in range(NX - 1):
                self.frames[str(fid)] = (grid[(ix, iy)], grid[(ix + 1, iy)], "90x12mm thk. FB")
                fid += 1
        for ix in range(NX):
            self.frames[str(fid)] = (grid[(ix, 0)], grid[(ix, 1)], "90x12mm thk. FB")
            fid += 1
        self.bearing = [f for f, (i, j, _) in self.frames.items() if self.joints[i][1] != self.joints[j][1]]
        sup_ix = (0, (NX - 1) // 2, NX - 1)
        self.restrained = {grid[(ix, iy)] for ix in sup_ix for iy in range(2)}
        self.selected: list[str] = []
        self.cases = ["DEAD", "MODAL", "LIVE"]
        self.combos = ["1.35G+1.50Q"]
        self.factors = {"DEAD": (1.0, 0.0), "LIVE": (0.0, 1.0), "1.35G+1.50Q": (1.35, 1.5)}

        m = self
        self.PointObj = _ns(
            Count=lambda: len(m.joints),
            GetNameList=lambda n, a: (0, len(m.joints), list(m.joints)),
            GetCoordCartesian=lambda nm, x, y, z, cs="Global": (0, *m.joints[nm]),
            GetRestraint=lambda nm, arr: (0, [True, True, True, False, False, False] if nm in m.restrained else [False] * 6),
            GetLoadForce=lambda *a: (0, 0, [], [], [], [], [], [], [], [], [], []),
        )
        self.FrameObj = _ns(
            Count=lambda: len(m.frames),
            GetNameList=lambda n, a: (0, len(m.frames), list(m.frames)),
            GetSection=lambda nm, a, b: (0, m.frames[nm][2], ""),
            GetPoints=lambda nm, a, b: (0, m.frames[nm][0], m.frames[nm][1]),
            GetLocalAxes=lambda nm, a, b: (0, 0.0, False),
            SetOutputStations=lambda *a: 0,
            GetModifiers=lambda nm, arr: (0, [1.0] * 8),
            GetLoadDistributed=self._get_load_distributed,
            GetLoadPoint=lambda *a: (0, 0, [], [], [], [], [], [], [], []),
        )
        self.AreaObj = _ns(Count=lambda: 0)
        self.PropFrame = _ns(
            GetSectProps=lambda nm, *a: (0, 0.00108, 0, 0, 0, 0.09 * 0.012 ** 3 / 12, 0.012 * 0.09 ** 3 / 12, 0, 0, 0, 0, 0, 0),
            GetMaterial=lambda nm, a: (0, "S275"),
            GetModifiers=lambda nm, arr: (0, [1.0] * 8),
        )
        self.PropMaterial = _ns(GetMPIsotropic=lambda nm, e, u, a: (0, 2.1e8, 0.3, 1.2e-5))
        self.File = _ns(OpenFile=lambda p: 0, NewBlank=lambda: 0)
        self.Analyze = _ns(RunAnalysis=lambda: 0)
        self.LoadCases = _ns(
            GetNameList=lambda n, a: (0, len(m.cases), list(m.cases)),
            # Real OAPI: (ret, CaseType, SubType). Modal = 3.
            GetTypeOAPI=lambda nm, t, st: (0, 3 if nm == "MODAL" else 1, 0),
        )
        self.LoadPatterns = _ns(GetNameList=lambda n, a: (0, 2, ["DEAD", "LIVE"]))
        self.RespCombo = _ns(GetNameList=lambda n, a: (0, len(m.combos), list(m.combos)))

        def _desel():
            m.selected = []

        self.Results = _ns(
            Setup=_ns(
                DeselectAllCasesAndCombosForOutput=_desel,
                SetCaseSelectedForOutput=lambda nm: m.selected.append(nm),
                SetComboSelectedForOutput=lambda nm: m.selected.append(nm),
            ),
            JointDispl=self._joint_displ,
            JointReact=self._joint_react,
            FrameForce=self._frame_force,
        )
        self.DesignSteel = _ns(
            StartDesign=lambda: 0,
            GetSummaryResults=self._design_summary,
            GetCode=lambda a: (0, "Eurocode 3-2005"),
        )

    # ── model API ───────────────────────────────────────────────────────────
    def SetPresentUnits(self, _u):
        return 0

    def SetModelIsLocked(self, _b):
        return 0

    def GetVersion(self, a, b):
        return (0, "25.3.1", 25.31)

    def _get_load_distributed(self, *a):
        names = list(self.bearing)
        n = len(names)
        return (0, n, names, ["LIVE"] * n, [1] * n, ["GLOBAL"] * n, [10] * n,
                [0.0] * n, [1.0] * n, [0.0] * n, [LY] * n, [W_LIVE] * n, [W_LIVE] * n)

    # ── synthetic results ───────────────────────────────────────────────────
    def _factor(self, case):
        if case == "MODAL":
            return None
        return self.factors[case]

    def _defl(self, x, y, fac):
        # edge bars sag a little between supports (all joints sit on the edges)
        g, q = fac
        w = 0.05 * g + W_LIVE * q
        d_mid = 5 * w * LY ** 4 / (384 * EI)
        return -0.1 * d_mid * abs(math.sin(2 * math.pi * x / LX))

    def _joint_displ(self, name, itype, n, *arrs):
        obj, elm, case, step, stepn, u1, u2, u3, r1, r2, r3 = ([] for _ in range(11))
        for c in self.selected:
            fac = self._factor(c)
            for j, (x, y, _) in self.joints.items():
                obj.append(j); elm.append(j); case.append(c); step.append(""); stepn.append(0.0)
                if fac is None:
                    u1.append(5.0); u2.append(3.0); u3.append(-11.25)
                else:
                    u1.append(0.0); u2.append(0.0); u3.append(self._defl(x, y, fac))
                r1.append(0.0); r2.append(0.0); r3.append(0.0)
        return (0, len(obj), obj, elm, case, step, stepn, u1, u2, u3, r1, r2, r3)

    def _joint_react(self, name, itype, n, *arrs):
        obj, elm, case, step, stepn = [], [], [], [], []
        f = [[] for _ in range(6)]
        for c in self.selected:
            fac = self._factor(c)
            for j in sorted(self.restrained):
                obj.append(j); elm.append(j); case.append(c); step.append(""); stepn.append(0.0)
                vals = [1.4e6, 0, 17444.0, 0, 0, 0] if fac is None else [0, 0, (0.05 * fac[0] + fac[1]) * 1.2, 0, 0, 0]
                for k in range(6):
                    f[k].append(vals[k])
        return (0, len(obj), obj, elm, case, step, stepn, *f)

    def _frame_force(self, name, itype, n, *arrs):
        cols = [[] for _ in range(13)]
        for c in self.selected:
            fac = self._factor(c)
            for fname, (i, j, _) in self.frames.items():
                L = math.dist(self.joints[i], self.joints[j])
                bearing = fname in self.bearing
                for k in range(N_STA):
                    s = L * k / (N_STA - 1)
                    if fac is None:
                        p, v2, m3 = 1.5e6, 7.1e4, 2.7e4
                    else:
                        w = (0.05 * fac[0] + W_LIVE * fac[1]) * (1.0 if bearing else 0.2)
                        p, v2, m3 = 0.0, w * (L / 2 - s), w * s * (L - s) / 2
                    row = [fname, s, fname, s, c, "", 0.0, p, v2, 0.0, 0.0, 0.0, m3]
                    for col, val in zip(cols, row):
                        col.append(val)
        return (0, len(cols[0]), *cols)

    def _design_summary(self, name, n, *arrs):
        names = list(self.frames)
        ratios = [0.215 if f in self.bearing else 0.89 for f in names]
        k = len(names)
        return (0, k, names, ratios, [1] * k, [0.5] * k, ["1.35G+1.50Q"] * k, [""] * k, [""] * k)
