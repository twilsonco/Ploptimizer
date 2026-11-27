"""Layout ink containment: a placement's engraving stays inside its slot.

The packer reserves a slot from the label's rendered footprint; the relief
engine, the border stroke and the text cascade then decide what is actually
engraved. This suite measures the engraved ink of every placement (read back
from the emitted per-cutter PLTs with the production parser, so relief passes,
sweeps and merge rewrites are all included) and pins two invariants:

1. the engraving lies inside the slot the packer reserved
   (``PackedLabel.x/y/width/height``), and
2. the plate-proof key (``usage_stats.placement_ink_rect``) describes that
   engraving -- the dashed key the operator drills by sits on the ink.

Every cell is green as of 2026-11-26: the assembly lands a rendered footprint's
own box on the slot (``packing.layout.placement_content_shift``), so a label
whose text block is taller than its nominal box, and a relief label whose fill
box is narrower than the footprint the packer reserved, both keep their
engraving -- and their plate-proof key -- inside the slot.

The rich per-label report (engraved box, slot, key, four signed gaps, the whole
variant matrix) lives in the gitignored dev probe ``.probe/js/relief_slot_vs_ink.py``;
this file keeps the same measurement in a form the suite can pin.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Tuple

import pytest

from plt_optimizer.core.models import ArcSegment, PLTDocument
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.units import units_to_inches
from plt_optimizer.generate import job_prep, vectorize
from plt_optimizer.generate.job_config import load_job_config
from plt_optimizer.generate.usage_stats import (
    SLOT_CENTRE_TOL_IN,
    SLOT_TOL_IN,
    placement_ink_rect,
)

REPO = Path(__file__).resolve().parents[1]
DEPS = REPO / "tests_deps"
JOB_CONFIG = REPO / "job-config.json"
TOOLS = REPO / "tools.json"

#: Overhang tolerance in inches. Every contained case measures 0.000; this is
#: the shop's smallest cutter scale, i.e. well inside any mechanical tolerance.
TOL_IN = 0.005


#: The module fixture's shape: ``spec_name -> export``, cached per fixture.
RunExport = Callable[[str], "vectorize.PerCutterExport"]


class Rect(NamedTuple):
    """An axis-aligned rectangle in inches: ``(x_min, x_max, y_min, y_max)``."""

    x_min: float
    x_max: float
    y_min: float
    y_max: float

    @classmethod
    def from_box(cls, x: float, y: float, width: float, height: float) -> Rect:
        """Build a rect from an origin and a size."""
        return cls(x, x + width, y, y + height)

    @property
    def center(self) -> Tuple[float, float]:
        """Return the rectangle's centre."""
        return ((self.x_min + self.x_max) / 2.0, (self.y_min + self.y_max) / 2.0)

    def padded(self, amount: float) -> Rect:
        """Return the rect grown by ``amount`` on every edge."""
        return Rect(
            self.x_min - amount,
            self.x_max + amount,
            self.y_min - amount,
            self.y_max + amount,
        )

    def covers(self, x: float, y: float) -> bool:
        """Return whether a point lies inside the rect."""
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max

    def union(self, other: Rect) -> Rect:
        """Return the bounding box of this rect and ``other``."""
        return Rect(
            min(self.x_min, other.x_min),
            max(self.x_max, other.x_max),
            min(self.y_min, other.y_min),
            max(self.y_max, other.y_max),
        )

    def overhang(self, inner: Rect) -> float:
        """Return how far ``inner`` sticks out past this rect, worst side."""
        return max(
            self.x_min - inner.x_min,
            inner.x_max - self.x_max,
            self.y_min - inner.y_min,
            inner.y_max - self.y_max,
        )


class SlotInk(NamedTuple):
    """One placement: its slot, its plate-proof key, its measured engraving."""

    label_id: str
    plate: int
    rotated: bool
    relief: bool
    slot: Rect
    key: Rect
    ink: Rect
    strokes: int


class Measurement(NamedTuple):
    """An export's per-placement ink plus the strokes no slot can claim."""

    slots: List[SlotInk]
    unattributed: Dict[int, List[Rect]]


def _shop_tools() -> Tuple[List[float], float]:
    """Return the shop cutter inventory and boundary/hole cutter from tools.json."""
    data = json.loads(TOOLS.read_text(encoding="utf-8"))
    return (
        [float(value) for value in data["available_cutters"]],
        float(data["boundary_hole_cutter_size"]),
    )


def _arc_points(seg: ArcSegment, steps: int = 12) -> List[Tuple[float, float]]:
    """Sample an arc into points, in device units."""
    radius = math.hypot(seg.start.x - seg.center.x, seg.start.y - seg.center.y)
    start = math.atan2(seg.start.y - seg.center.y, seg.start.x - seg.center.x)
    sweep = math.radians(seg.sweep_angle)
    return [
        (
            seg.center.x + radius * math.cos(start + sweep * (i / steps)),
            seg.center.y + radius * math.sin(start + sweep * (i / steps)),
        )
        for i in range(steps + 1)
    ]


def _stroke_boxes(document: PLTDocument) -> List[Rect]:
    """Return every cutting stroke's bbox, in inches."""
    boxes: List[Rect] = []
    for path in document.stroke_paths:
        xs: List[float] = []
        ys: List[float] = []
        for seg in path.segments:
            if not seg.is_cutting:
                continue
            points = [(seg.start.x, seg.start.y), (seg.end.x, seg.end.y)]
            if isinstance(seg, ArcSegment):
                points = _arc_points(seg)
            xs.extend(units_to_inches(x) for x, _y in points)
            ys.extend(units_to_inches(y) for _x, y in points)
        if xs:
            boxes.append(Rect(min(xs), max(xs), min(ys), max(ys)))
    return boxes


def _run(spec_name: str, out_dir: Path) -> vectorize.PerCutterExport:
    """Run a fixture through the production generate path (no optimisation)."""
    cutters, boundary = _shop_tools()
    config = load_job_config(JOB_CONFIG)
    prep = job_prep.prepare_job(
        DEPS / spec_name,
        job_config=config,
        job_config_path=JOB_CONFIG,
        inventory=list(cutters),
        boundary_hole_cutter=boundary,
    )
    return vectorize.export_per_cutter_plts(
        prep.resolved_labels,
        prep.job.plates,
        output_dir=out_dir,
        job_id="layout_ink",
        name="layout_ink",
        optimize=False,
        plots=False,
        default_plots=False,
        allow_rotation=prep.job.allow_rotation,
        layout=prep.job.layout,
        default_plate_size=(20.0, 20.0),
        default_plate_clearance=(0.0, 0.0),
        job_spec=prep.job,
        job_config=config,
        available_cutters=list(cutters),
    )


def _measure(result: vectorize.PerCutterExport) -> Measurement:
    """Attribute every engraved stroke of an export to its placement slot.

    Attribution mirrors ``usage_stats``: a stroke belongs to the placement whose
    plate-proof key (padded by ``SLOT_TOL_IN``) holds its centre, else the
    nearest key within ``SLOT_CENTRE_TOL_IN``. Strokes no key can claim -- what
    an engraving that left its slot looks like to the accounting layer -- are
    returned per plate instead of being forced onto a placement.
    """
    frames: Dict[int, List[SlotInk]] = {}
    for index, plate in enumerate(result.packed_plates, start=1):
        number = int(getattr(plate, "plate_number", index) or index)
        frames[number] = [
            SlotInk(
                label_id=str(getattr(placement, "label_id", "") or ""),
                plate=number,
                rotated=bool(placement.rotated),
                relief=bool(getattr(placement.source_label, "relief_engraving", False)),
                slot=Rect.from_box(
                    float(placement.x),
                    float(placement.y),
                    float(placement.width),
                    float(placement.height),
                ),
                key=Rect.from_box(*placement_ink_rect(placement)),
                ink=Rect(math.inf, -math.inf, math.inf, -math.inf),
                strokes=0,
            )
            for placement in plate.labels
        ]
    unattributed: Dict[int, List[Rect]] = {number: [] for number in frames}
    plate_numbers = list(frames)
    parser = PLTParser()
    for path in sorted(result.plt_paths):
        if "_default_" in path.name:
            continue
        stem = vectorize._parse_plt_stem(path.name)
        number = (
            int(stem.plate)
            if stem.plate is not None
            else (plate_numbers[0] if len(plate_numbers) == 1 else -1)
        )
        slots = frames.get(number, [])
        for box in _stroke_boxes(parser.parse_file(path)):
            centre = box.center
            pool = [s for s in slots if s.key.padded(SLOT_TOL_IN).covers(*centre)] or [
                s for s in slots if s.key.padded(SLOT_CENTRE_TOL_IN).covers(*centre)
            ]
            if not pool:
                unattributed.setdefault(number, []).append(box)
                continue
            owner = min(
                pool,
                key=lambda s: (
                    (s.key.center[0] - centre[0]) ** 2 + (s.key.center[1] - centre[1]) ** 2
                ),
            )
            frames[number][frames[number].index(owner)] = owner._replace(
                ink=owner.ink.union(box),
                strokes=owner.strokes + 1,
            )
    return Measurement(
        slots=[slot for slots in frames.values() for slot in slots],
        unattributed=unattributed,
    )


def _slot_map(
    run: RunExport,
    spec_name: str,
) -> Dict[str, SlotInk]:
    """Return every placement of a fixture keyed by ``plate/label``."""
    return {f"{s.plate}/{s.label_id}": s for s in _measure(run(spec_name)).slots}


# ---------------------------------------------------------------------------
# The invariant: engraved ink inside the reserved slot
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ink_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> RunExport:
    """Return ``spec_name -> export``, running each fixture once per module.

    Every case measures the same exports, and a relief export is a full
    generate pass (render, relief ladder, per-cutter export), so the cache is
    what keeps this file's runtime sane.
    """
    root = tmp_path_factory.mktemp("layout_ink")
    cache: Dict[str, vectorize.PerCutterExport] = {}

    def run(spec_name: str) -> vectorize.PerCutterExport:
        """Return the cached export of a ``tests_deps`` fixture."""
        if spec_name not in cache:
            cache[spec_name] = _run(spec_name, root / Path(spec_name).stem)
        return cache[spec_name]

    return run


def test_baseline_relief_label_engraves_inside_its_slot(ink_run: RunExport) -> None:
    """A plain relief label's engraving fills -- and stays inside -- its slot."""
    slots = _slot_map(ink_run, "layout_ink_baseline.yaml")
    slot = slots["1/baseline_0"]
    assert slot.relief
    assert slot.strokes > 0
    assert slot.slot.overhang(slot.ink) <= TOL_IN


def test_relief_neighbours_each_stay_inside_their_slots(ink_run: RunExport) -> None:
    """Rect labels pack coincident, so each relief label's ink stays in its own slot."""
    slots = _slot_map(ink_run, "layout_ink_pair_coincident.yaml")
    assert len(slots) == 3
    for slot in slots.values():
        assert slot.slot.overhang(slot.ink) <= TOL_IN, slot.label_id


def test_text_overflow_stays_inside_the_reserved_slot(ink_run: RunExport) -> None:
    """A label whose text block exceeds its nominal box keeps its ink in the slot.

    The packer reserves the rendered footprint (the text block, taller than the
    nominal box) and the assembly lands that footprint's own box on the slot,
    so nothing engraves below the slot -- the case that used to hang ~0.79in of
    a 3-line block under the plate origin.
    """
    slot = _slot_map(ink_run, "layout_ink_text_overflow.yaml")["1/overflow_0"]
    assert slot.slot.overhang(slot.ink) <= TOL_IN


def test_relief_with_text_overflow_stays_inside_the_reserved_slot(ink_run: RunExport) -> None:
    """Relief plus overflowing text keeps its engraving inside the reserved slot.

    The relief fill is the nominal box and the slot is the taller rendered
    footprint, so the raster lands inside it with room to spare -- the case the
    2026-11-25 TODO entry described as an overhang.
    """
    slot = _slot_map(ink_run, "layout_ink_relief_overflow.yaml")["1/relief_overflow_0"]
    assert slot.relief
    assert slot.slot.overhang(slot.ink) <= TOL_IN, (
        f"slot {slot.slot} vs ink {slot.ink} (key {slot.key}, {slot.strokes} strokes)"
    )


# ---------------------------------------------------------------------------
# The plate-proof key describes the engraving
# ---------------------------------------------------------------------------


def test_key_sits_on_the_engraving_for_an_upright_label(ink_run: RunExport) -> None:
    """The plate-proof key of an upright relief label matches its engraved box."""
    slot = _slot_map(ink_run, "layout_ink_baseline.yaml")["1/baseline_0"]
    assert _key_offset(slot) <= TOL_IN


def test_key_sits_on_the_engraving_for_a_rotated_relief_label(ink_run: RunExport) -> None:
    """A rotated relief label's plate-proof key sits on its engraving.

    Rotation is origin-normalised by :func:`rotate_plt_content_90cw`, so a
    rotated label lands in its slot the same way an upright one does and the
    turn does not throw the key off.
    """
    slots = _slot_map(ink_run, "layout_ink_relief_rotated.yaml")
    slot = slots["1/rotated_0"]
    assert slot.rotated
    assert _key_offset(slot) <= TOL_IN


def _key_offset(slot: SlotInk) -> float:
    """Return how far a placement's plate-proof key sits off its engraved box."""
    return max(
        abs(slot.key.x_min - slot.ink.x_min),
        abs(slot.key.x_max - slot.ink.x_max),
        abs(slot.key.y_min - slot.ink.y_min),
        abs(slot.key.y_max - slot.ink.y_max),
    )


def test_key_sits_on_the_engraving_when_the_ink_is_narrower_than_the_slot(
    ink_run: RunExport,
) -> None:
    """A relief label's key sits on its engraving when the ink is off-centre.

    Here the relief fill is the 1.0in nominal box while the packer reserved the
    taller rendered text footprint, so a key centred in the slot and an engraving
    anchored at its origin disagree -- the assembly resolves that by landing the
    content's own box on the slot origin.
    """
    slot = _slot_map(ink_run, "layout_ink_relief_ladder.yaml")["1/ladder_0"]
    assert _key_offset(slot) <= TOL_IN, (
        f"key {slot.key} vs ink {slot.ink} (slot {slot.slot}, {slot.strokes} strokes)"
    )


# ---------------------------------------------------------------------------
# The plate keeps its engraving inside the usable area
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec_name",
    [
        "layout_ink_baseline.yaml",
        "layout_ink_pair_coincident.yaml",
        "layout_ink_relief_rotated.yaml",
        "layout_ink_text_overflow.yaml",
        "layout_ink_relief_overflow.yaml",
    ],
)
def test_plate_keeps_its_engraving_inside_the_usable_area(
    ink_run: RunExport,
    spec_name: str,
) -> None:
    """Nothing engraves past the plate's usable area (plate size less clearances)."""
    result = ink_run(spec_name)
    cutters, boundary = _shop_tools()
    assert cutters and boundary > 0.0
    measurement = _measure(result)
    usable: Dict[int, Rect] = {}
    for index, plate in enumerate(result.packed_plates, start=1):
        number = int(getattr(plate, "plate_number", index) or index)
        usable[number] = Rect.from_box(
            float(plate.left_clearance),
            float(plate.top_clearance),
            float(plate.width),
            float(plate.height),
        )
    ink_by_plate: Dict[int, Rect] = {}

    def _grow(plate_number: int, box: Rect) -> None:
        current = ink_by_plate.get(plate_number)
        ink_by_plate[plate_number] = box if current is None else current.union(box)

    for slot in measurement.slots:
        _grow(slot.plate, slot.ink)
    # Strokes no slot can claim still came off this plate's file.
    for plate_number, boxes in measurement.unattributed.items():
        for box in boxes:
            _grow(plate_number, box)
    for plate_number, ink in ink_by_plate.items():
        assert usable[plate_number].overhang(ink) <= TOL_IN, (
            f"plate {plate_number} of {spec_name} engraves outside its usable area: "
            f"usable {usable[plate_number]} vs ink {ink}"
        )
