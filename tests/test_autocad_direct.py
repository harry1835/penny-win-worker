"""Unit tests for direct AutoCAD COM extract & base-plate draw helpers."""
from __future__ import annotations

from types import SimpleNamespace

from engines.autocad_engine import (
    _draw_baseplate_on_com_doc,
    _extract_entities_from_com_doc,
)


class _FakeModelSpace:
    def __init__(self, items=None) -> None:
        self._items = list(items or [])
        self.added_lines = []
        self.added_circles = []
        self.added_texts = []

    @property
    def Count(self) -> int:
        return len(self._items)

    def Item(self, idx: int):
        return self._items[idx]

    def AddLine(self, p1, p2):
        obj = SimpleNamespace(p1=p1, p2=p2, Layer="0")
        self.added_lines.append(obj)
        return obj

    def AddCircle(self, center, radius):
        obj = SimpleNamespace(center=center, radius=radius, Layer="0")
        self.added_circles.append(obj)
        return obj

    def AddText(self, text, pt, height):
        obj = SimpleNamespace(text=text, pt=pt, height=height, Layer="0")
        self.added_texts.append(obj)
        return obj


class _FakeLayers:
    def __init__(self) -> None:
        self.names = []

    def Add(self, name: str):
        self.names.append(name)
        return SimpleNamespace(name=name, color=7)


def test_extract_entities_from_com_doc():
    entities = [
        SimpleNamespace(ObjectName="AcDbLine", Layer="S-STEEL", StartPoint=(0, 0, 0), EndPoint=(6000, 0, 0)),
        SimpleNamespace(ObjectName="AcDbPolyline", Layer="S-BEAM", Coordinates=[0, 0, 0, 4000, 4000, 4000], Closed=False),
        SimpleNamespace(ObjectName="AcDbText", Layer="S-TEXT", TextString="UB 305x165x40", InsertionPoint=(3000, 100, 0)),
        SimpleNamespace(ObjectName="AcDbRotatedDimension", Layer="DIM", Measurement=6000.0, TextOverride="6000"),
    ]
    doc = SimpleNamespace(
        GetVariable=lambda k: 4 if k == "INSUNITS" else 0,
        ModelSpace=_FakeModelSpace(entities),
    )

    out = _extract_entities_from_com_doc(doc)
    assert out["insunits"] == 4
    assert out["engine"] == "autocad_com"
    assert len(out["lines"]) == 3  # 1 line + 2 polyline segments
    assert out["texts"][0]["text"] == "UB 305x165x40"
    assert out["dimensions"][0]["measured"] == 6000.0
    assert "S-STEEL" in out["layers"]


def test_draw_baseplate_on_com_doc():
    msp = _FakeModelSpace()
    layers = _FakeLayers()
    doc = SimpleNamespace(ModelSpace=msp, Layers=layers)

    report = {
        "anchor_type": "HST2 V3 M10",
        "anchor_diameter_mm": 10.0,
        "hef_mm": 44.0,
        "hnom_mm": 54.0,
        "plate_L_mm": 150.0,
        "plate_B_mm": 150.0,
        "plate_t_mm": 12.0,
        "profile": "SHS 50x50x5",
        "profile_h_mm": 50.0,
        "profile_b_mm": 50.0,
        "concrete_grade": "C20/25",
        "slab_thickness_mm": 150.0,
    }
    geom = {
        "coords_mm": [[-60, -60], [60, -60], [60, 60], [-60, 60]],
        "spacing_x_mm": 120.0,
        "spacing_y_mm": 120.0,
    }

    _draw_baseplate_on_com_doc(doc, report, geom)
    assert len(msp.added_circles) == 4
    assert len(msp.added_lines) >= 40
    texts = [t.text for t in msp.added_texts]
    assert any("1. PLAN VIEW" in t for t in texts)
    assert any("4. SECTION A-A" in t for t in texts)
    assert any("TO BE CONFIRMED" in t for t in texts)
