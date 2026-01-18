# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

"""
Calculate bounding boxes for part symbols and hierarchical sheets.
"""

from collections import namedtuple

from skidl.logger import active_logger
from skidl.geometry import (
    Tx,
    BBox,
    Point,
    Vector,
    tx_rot_0,
    tx_rot_90,
    tx_rot_180,
    tx_rot_270,
    mils_per_mm,
    mms_per_mil,
)
from skidl.utilities import export_to_all
from .constants import HIER_TERM_SIZE, PIN_LABEL_FONT_SIZE
from .gen_svg import draw_cmd_to_svg
from skidl.geometry import BBox, Point, Tx, Vector


@export_to_all
def calc_symbol_bbox(part, **options):
    """
    Return the bounding box of the part symbol.

    Args:
        part: Part object for which an SVG symbol will be created.
        options (dict): Various options to control bounding box calculation:
            graphics_only (boolean): If true, compute bbox of graphics (no text).

    Returns: List of BBoxes for all units in the part symbol.
    """

    unit_nums = [getattr(unit, "num", 1) for unit in part.unit.values()] or [1]
    max_unit = max(unit_nums)
    unit_bboxes = [BBox() for _ in range(max_unit + 1)]

    for unit in part.unit.values():
        unit_num = getattr(unit, "num", 1)
        unit_bbox = BBox()
        for cmd in part.draw_cmds.get(unit_num, []):
            _, cmd_bbox = draw_cmd_to_svg(cmd, Tx(), part, [], 0)
            unit_bbox.add(cmd_bbox)
        unit_bbox *= mils_per_mm
        unit_bbox = unit_bbox.round()
        unit.bbox = unit_bbox
        unit_bboxes[unit_num] = unit_bbox

    return unit_bboxes


@export_to_all
def calc_hier_label_bbox(label, dir):
    """Calculate the bounding box for a hierarchical label.

    Args:
        label (str): String for the label.
        dir (str): Orientation ("U", "D", "L", "R").

    Returns:
        BBox: Bounding box for the label and hierarchical terminal.
    """

    # Rotation matrices for each direction.
    lbl_tx = {
        "U": tx_rot_90,  # Pin on bottom pointing upwards.
        "D": tx_rot_270,  # Pin on top pointing down.
        "L": tx_rot_180,  # Pin on right pointing left.
        "R": tx_rot_0,  # Pin on left pointing right.
    }

    # Calculate length and height of label + hierarchical marker.
    lbl_len = len(label) * PIN_LABEL_FONT_SIZE + HIER_TERM_SIZE
    lbl_hgt = max(PIN_LABEL_FONT_SIZE, HIER_TERM_SIZE)

    # Create bbox for label on left followed by marker on right.
    bbox = BBox(Point(0, lbl_hgt / 2), Point(-lbl_len, -lbl_hgt / 2))

    # Rotate the bbox in the given direction.
    bbox *= lbl_tx[dir]

    return bbox
