# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.


import copy
import datetime
import os
import os.path
import time
import uuid
from collections import Counter, OrderedDict

from simp_sexp import Sexp
from skidl.scriptinfo import get_script_name
from skidl.geometry import BBox, Point, Tx, Vector, mils_per_mm, mms_per_mil
from skidl.schematics.net_terminal import NetTerminal
from skidl.utilities import export_to_all, rmv_attr
from .constants import BLK_INT_PAD, BOX_LABEL_FONT_SIZE, GRID, PIN_LABEL_FONT_SIZE
from .bboxes import calc_symbol_bbox, calc_hier_label_bbox
from .gen_netlist import namespace_uuid, gen_part_tstamp, gen_sheetpath_tstamp


__all__ = []

"""
Functions for generating a KiCad 9 schematic using s-expressions.
"""

# Cached symbol definitions per library file.
_LIB_SYMBOL_CACHE = {}

# KiCad 9 schematic page sizes in mm
A_SIZES_MM = OrderedDict([
    ("A4", (210, 297)),
    ("A3", (297, 420)),
    ("A2", (420, 594)),
    ("A1", (594, 841)),
    ("A0", (841, 1189)),
])


def get_skidl_version():
    """Get SKiDL version string."""
    try:
        from skidl import __version__
        return __version__
    except ImportError:
        return "unknown"


# Removed manual symbol creation functions - now using SKiDL's draw_cmds data


def part_to_lib_symbol_definition(part):
    """Extract library symbol definition from SKiDL part using its draw_cmds.

    Args:
        part: SKiDL Part object with draw_cmds data

    Returns:
        list: Nested list representing the complete library symbol definition
    """
    # Get library and part name
    lib_name = os.path.splitext(part.lib.filename)[0] if hasattr(part.lib, 'filename') and part.lib.filename else "Device"
    part_name = part.name or "Unknown"
    lib_id = f"{lib_name}:{part_name}"

    # Start building the symbol definition
    symbol_def = [
        "symbol", lib_id,
        ["pin_numbers", ["hide", "yes"]],
        ["pin_names", ["offset", 0]],
        ["exclude_from_sim", "no"],
        ["in_bom", "yes"],
        ["on_board", "yes"]
    ]

    # Add properties from the part
    symbol_def.extend([
        ["property", "Reference", part.ref_prefix or "U",
            ["at", 2.032, 0, 90],
            ["effects", ["font", ["size", 1.27, 1.27]]]
        ],
        ["property", "Value", part_name,
            ["at", 0, 0, 90],
            ["effects", ["font", ["size", 1.27, 1.27]]]
        ],
        ["property", "Footprint", "",
            ["at", 0, 0, 0],
            ["effects", ["font", ["size", 1.27, 1.27]], ["hide", "yes"]]
        ],
        ["property", "Datasheet", part.datasheet or "~",
            ["at", 0, 0, 0],
            ["effects", ["font", ["size", 1.27, 1.27]], ["hide", "yes"]]
        ]
    ])

    # Add description if available
    if hasattr(part, 'description') and part.description:
        symbol_def.append([
            "property", "Description", part.description,
            ["at", 0, 0, 0],
            ["effects", ["font", ["size", 1.27, 1.27]], ["hide", "yes"]]
        ])

    # Add keywords if available
    if hasattr(part, 'keywords') and part.keywords:
        symbol_def.append([
            "property", "ki_keywords", part.keywords,
            ["at", 0, 0, 0],
            ["effects", ["font", ["size", 1.27, 1.27]], ["hide", "yes"]]
        ])

    # Add footprint filters if available
    if hasattr(part, 'fplist') and part.fplist:
        fplist_str = " ".join(part.fplist)
        symbol_def.append([
            "property", "ki_fp_filters", fplist_str,
            ["at", 0, 0, 0],
            ["effects", ["font", ["size", 1.27, 1.27]], ["hide", "yes"]]
        ])

    # Process drawing commands from the part
    # draw_cmds is a defaultdict with keys for units (0=common, 1,2,3...=units)

    # Add common graphics (unit 0) if present
    if 0 in part.draw_cmds:
        graphics_cmds = [copy.deepcopy(cmd) for cmd in part.draw_cmds[0] if cmd[0] not in ['pin']]
        if graphics_cmds:
            symbol_def.append([
                "symbol", f"{part_name}_0_1"
            ] + graphics_cmds)

    # Add unit-specific graphics and pins
    for unit_num, draw_cmds in part.draw_cmds.items():
        if unit_num == 0:
            continue  # Already processed above

        # Separate pins from graphics and convert Sexp objects to lists
        pin_cmds = [copy.deepcopy(cmd) for cmd in draw_cmds if cmd[0] == 'pin']
        graphics_cmds = [copy.deepcopy(cmd) for cmd in draw_cmds if cmd[0] not in ['pin', 'property']]

        if pin_cmds or graphics_cmds:
            unit_symbol = ["symbol", f"{part_name}_{unit_num}_{unit_num}"]
            unit_symbol.extend(graphics_cmds)
            unit_symbol.extend(pin_cmds)
            symbol_def.append(unit_symbol)

    # Add embedded_fonts
    symbol_def.append(["embedded_fonts", "no"])

    return symbol_def


def get_lib_symbol_definition_from_part(part):
    """Get library symbol definition using SKiDL part's actual draw_cmds data.

    Args:
        part: SKiDL Part object

    Returns:
        list: List of nested lists representing library symbol definitions
    """
    symbol_defs = _get_lib_symbol_definitions_from_library(part)
    if symbol_defs:
        return symbol_defs
    return [part_to_lib_symbol_definition(part)]


def _get_lib_symbol_definitions_from_library(part):
    lib = getattr(part, "lib", None)
    lib_path = getattr(lib, "filepath", None)
    if not lib_path or not os.path.isfile(lib_path):
        return None

    symbols = _load_library_symbols(lib_path)
    if not symbols:
        return None

    symbol_name = part.name
    if symbol_name not in symbols and getattr(part, "aliases", None):
        for alias in part.aliases:
            if alias in symbols:
                symbol_name = alias
                break

    if symbol_name not in symbols:
        return None

    lib_name = os.path.splitext(getattr(lib, "filename", "") or os.path.basename(lib_path))[0]
    return _collect_symbol_defs_with_extends(symbols, symbol_name, lib_name)


def _load_library_symbols(lib_path):
    cached = _LIB_SYMBOL_CACHE.get(lib_path)
    if cached is not None:
        return cached

    try:
        with open(lib_path, "rb") as f:
            lib_txt = f.read()
    except OSError:
        _LIB_SYMBOL_CACHE[lib_path] = None
        return None

    try:
        lib_txt = lib_txt.decode("latin_1")
    except AttributeError:
        pass

    try:
        lib_sexp = Sexp(lib_txt)
    except Exception:
        _LIB_SYMBOL_CACHE[lib_path] = None
        return None

    symbols = OrderedDict(
        (symbol[1], symbol)
        for symbol in lib_sexp.search("/kicad_symbol_lib/symbol", ignore_case=True)
    )
    _LIB_SYMBOL_CACHE[lib_path] = symbols
    return symbols


def _collect_symbol_defs_with_extends(symbols, symbol_name, lib_name, seen=None):
    if seen is None:
        seen = set()
    if symbol_name in seen:
        return []

    symbol = symbols.get(symbol_name)
    if not symbol:
        return []

    symbol_copy = copy.deepcopy(symbol)
    parent_name = None
    for item in symbol_copy:
        if isinstance(item, list) and item and item[0].lower() == "extends":
            parent_name = item[1]
            item[1] = f"{lib_name}:{item[1]}"
            break

    defs = []
    if parent_name:
        defs.extend(_collect_symbol_defs_with_extends(symbols, parent_name, lib_name, seen))

    symbol_copy[1] = f"{lib_name}:{symbol_copy[1]}"
    defs.append(symbol_copy)
    seen.add(symbol_name)
    return defs


def _round_mm(value, digits=4):
    return round(float(value), digits)


def _tx_to_kicad_orientation(tx):
    tx = tx.no_translate()
    key = (
        int(round(tx.a)),
        int(round(tx.b)),
        int(round(tx.c)),
        int(round(tx.d)),
    )

    rotation_map = {
        (1, 0, 0, 1): 0,
        (0, 1, -1, 0): 90,
        (-1, 0, 0, -1): 180,
        (0, -1, 1, 0): 270,
    }
    mirror_y_map = {
        (-1, 0, 0, 1): 0,
        (0, 1, 1, 0): 90,
        (1, 0, 0, -1): 180,
        (0, -1, -1, 0): 270,
    }
    mirror_x_map = {
        (1, 0, 0, -1): 0,
        (0, -1, -1, 0): 90,
        (-1, 0, 0, 1): 180,
        (0, 1, 1, 0): 270,
    }

    if key in rotation_map:
        return rotation_map[key], None
    if key in mirror_y_map:
        return mirror_y_map[key], "y"
    if key in mirror_x_map:
        return mirror_x_map[key], "x"

    return 0, None


def _calc_pin_dir(pin):
    tx = pin.part.tx.no_translate()
    pin_vector = {
        "U": Point(0, 1),
        "D": Point(0, -1),
        "L": Point(-1, 0),
        "R": Point(1, 0),
    }[pin.orientation]
    pin_vector = pin_vector * tx
    pin_vector = (int(round(pin_vector.x)), int(round(pin_vector.y)))
    return {
        (0, 1): "U",
        (0, -1): "D",
        (-1, 0): "L",
        (1, 0): "R",
    }[pin_vector]


def _net_label_kind(pin):
    net = pin.net
    pin_hiertuple = pin.part.hiertuple
    label_kind = "hierarchical_label"
    for pn in net.pins:
        pn_hiertuple = pn.part.hiertuple
        if pin_hiertuple[: len(pn_hiertuple)] == pn_hiertuple:
            continue
        if pn_hiertuple[: len(pin_hiertuple)] == pin_hiertuple:
            continue
        label_kind = "global_label"
        break

    if label_kind != "global_label":
        hiertuples = {p.part.hiertuple for p in net.pins}
        if len(hiertuples) <= 1:
            label_kind = "label"

    if label_kind == "label" and (pin.stub or isinstance(pin.part, NetTerminal)):
        label_kind = "global_label"

    return label_kind


def _net_label_shape(net):
    netio = getattr(net, "netio", "").lower()
    return {
        "i": "input",
        "o": "output",
        "b": "bidirectional",
        "t": "tri_state",
        "p": "passive",
    }.get(netio[:1], "input")


def pin_label_to_sexp(pin, tx):
    if not pin.is_connected():
        return None

    is_net_terminal = isinstance(pin.part, NetTerminal)
    if not (is_net_terminal or pin.stub):
        return None

    label_kind = _net_label_kind(pin)
    part_tx = pin.part.tx * tx
    pt = pin.pt * part_tx
    pin_dir = _calc_pin_dir(pin)
    angle = {"R": 0, "D": 90, "L": 180, "U": 270}[pin_dir]
    justify = "left" if angle in (0, 90) else "right"
    font_size = _round_mm(PIN_LABEL_FONT_SIZE * mms_per_mil)

    label_list = [label_kind, pin.net.name]
    if label_kind != "label":
        label_list.append(["shape", _net_label_shape(pin.net)])
    label_list.extend(
        [
            ["at", _round_mm(pt.x), _round_mm(pt.y), angle],
            [
                "effects",
                ["font", ["size", font_size, font_size]],
                ["justify", justify] + ([] if label_kind != "label" else ["bottom"]),
            ],
            ["uuid", str(uuid.uuid4())],
        ]
    )
    return label_list


def part_to_symbol_sexp(part, tx, main_sheet_uuid, sheet_uuids=None):
    """Convert a SKiDL part to a KiCad 9 symbol s-expression as nested list.

    Args:
        part: SKiDL Part object
        tx: Transformation matrix
        main_sheet_uuid: UUID of the main/root schematic sheet
        sheet_uuids: Dict mapping subcircuit names to their sheet UUIDs

    Returns:
        list: Nested list representing symbol s-expression
    """
    if not part.ref:
        return None

    tx = tx or Tx()

    # Transform part position
    origin = (part.tx * tx).origin
    pos_x = _round_mm(origin.x)
    pos_y = _round_mm(origin.y)
    angle, mirror = _tx_to_kicad_orientation(part.tx)

    # Generate UUID using same scheme as netlist generator
    symbol_uuid = gen_part_tstamp(part)

    # Get library and part name
    lib_name = os.path.splitext(part.lib.filename)[0] if hasattr(part.lib, 'filename') and part.lib.filename else "Device"
    part_name = part.name or "R"

    # Build proper hierarchical instance path for arbitrary depth
    if sheet_uuids is None:
        sheet_uuids = {}

    # Get the hierarchical path (skip root empty string)
    path_components = [level for level in part.hiertuple[1:] if level]

    if not path_components:
        # Root level part - path is just main sheet UUID
        instance_path = f"/{main_sheet_uuid}"
    else:
        # Multi-level hierarchical path: main_sheet_uuid/level1_uuid/level2_uuid/.../current_sheet_uuid
        path_uuids = [main_sheet_uuid]

        # Build path incrementally for each hierarchy level INCLUDING the current level
        for i in range(len(path_components)):
            # Create the path key for this level (e.g., "power" or "power/regulators")
            level_path = "/".join(path_components[:i+1])
            level_uuid = sheet_uuids.get(level_path)

            if level_uuid:
                path_uuids.append(level_uuid)
            else:
                # Cannot find UUID for this level, stop building path
                break

        instance_path = "/" + "/".join(path_uuids)

    unit_num = getattr(part, "num", 1)

    # Create symbol as nested list
    symbol_list = [
        "symbol",
        ["lib_id", f"{lib_name}:{part_name}"],
        ["at", pos_x, pos_y, angle],
        ["unit", unit_num],
        ["exclude_from_sim", "no"],
        ["in_bom", "yes"],
        ["on_board", "yes"],
        ["dnp", "no"],
        ["uuid", symbol_uuid],
        # Reference property
        ["property", "Reference", part.ref,
            ["at", pos_x, _round_mm(pos_y - 2.54), 0],
            ["effects", ["font", ["size", 1.27, 1.27]]]
        ],
        # Value property
        ["property", "Value", str(part.value) if part.value else part.name,
            ["at", pos_x, _round_mm(pos_y + 2.54), 0],
            ["effects", ["font", ["size", 1.27, 1.27]]]
        ],
        # Footprint property (hidden)
        ["property", "Footprint", getattr(part, 'footprint', ''),
            ["at", pos_x, pos_y, 0],
            ["effects", ["font", ["size", 1.27, 1.27]], ["hide"]]
        ],
        # Datasheet property (hidden)
        ["property", "Datasheet", getattr(part, 'datasheet', '') or "",
            ["at", pos_x, pos_y, 0],
            ["effects", ["font", ["size", 1.27, 1.27]], ["hide"]]
        ],
        # Description property (hidden)
        ["property", "Description", getattr(part, 'description', '') or "",
            ["at", pos_x, pos_y, 0],
            ["effects", ["font", ["size", 1.27, 1.27]], ["hide"]]
        ]
    ]

    if mirror:
        symbol_list.append(["mirror", mirror])

    # Add all fields from part.fields dictionary (auto-export custom properties)
    y_offset = 5.08  # Start with 0.2 inch offset below the part
    if hasattr(part, 'fields') and part.fields:
        for field_name, field_value in part.fields.items():
            # Skip standard KiCad properties that we already handled above
            if field_name.lower() in ['reference', 'value', 'footprint', 'datasheet', 'description']:
                continue

            # Only add fields with non-empty values
            if field_value and str(field_value).strip():
                field_property = [
                    "property", field_name, str(field_value),
                    ["at", pos_x, _round_mm(pos_y + y_offset), 0],
                    ["effects", ["font", ["size", 1.27, 1.27]], ["hide"]]
                ]
                symbol_list.append(field_property)
                y_offset += 1.27  # Increment position for next field

    # Add other common part attributes as properties (if they exist and aren't already covered)
    additional_attrs = {
        'keywords': 'ki_keywords',
        'manufacturer': 'Manufacturer',
        'manf_num': 'MPN',
        'supplier': 'Supplier',
        'supplier_num': 'SPN',
        'tolerance': 'Tolerance',
        'temp_range': 'Temperature',
        'voltage_rating': 'Voltage',
        'power_rating': 'Power',
        'package': 'Package'
    }

    for attr_name, prop_name in additional_attrs.items():
        attr_value = getattr(part, attr_name, None)
        if attr_value and str(attr_value).strip():
            # Check if this property name already exists in fields to avoid duplicates
            if not (hasattr(part, 'fields') and prop_name in part.fields):
                additional_property = [
                    "property", prop_name, str(attr_value),
                    ["at", pos_x, _round_mm(pos_y + y_offset), 0],
                    ["effects", ["font", ["size", 1.27, 1.27]], ["hide"]]
                ]
                symbol_list.append(additional_property)
                y_offset += 1.27

    # Add the instances section
    symbol_list.extend([
        # Instances section - critical for KiCad to show references correctly
        ["instances",
            ["project", "SKiDL-Generated",
                ["path", instance_path,
                    ["reference", part.ref],
                    ["unit", unit_num]
                ]
            ]
        ]
    ])

    return symbol_list


def net_to_wire_sexp(net, wire_segments, tx):
    """Convert wire segments to KiCad 9 wire s-expressions as nested lists.

    Args:
        net: SKiDL Net object
        wire_segments: List of wire segments
        tx: Transformation matrix

    Returns:
        list: List of nested lists representing wire s-expressions
    """
    tx = tx or Tx()
    wires = []

    for segment in wire_segments:
        # Transform segment points
        transformed_segment = segment * tx
        start = transformed_segment.p1
        end = transformed_segment.p2

        wire_uuid = str(uuid.uuid4())

        wire_list = [
            "wire",
            ["pts",
                ["xy", _round_mm(start.x), _round_mm(start.y)],
                ["xy", _round_mm(end.x), _round_mm(end.y)]
            ],
            ["stroke", ["width", 0], ["type", "default"]],
            ["uuid", wire_uuid]
        ]
        wires.append(wire_list)

    return wires


def create_title_block_sexp(title):
    """Create title block s-expression as nested list.

    Args:
        title: Title string for the schematic

    Returns:
        list: Nested list representing title block
    """
    return [
        "title_block",
        ["title", title],
        ["date", datetime.date.today().isoformat()],
        ["company", ""],
        ["comment", 1, "Generated with SKiDL"],
        ["comment", 2, ""],
        ["comment", 3, ""],
        ["comment", 4, ""]
    ]


def create_hierarchical_sheet_sexp(node_name, sheet_filename, position, size, sheet_uuid):
    """Create a hierarchical sheet s-expression for KiCad9.

    Args:
        node_name: Name of the subcircuit/node
        sheet_filename: Filename of the sheet (e.g., "power.kicad_sch")
        position: (x, y) position tuple
        size: (width, height) size tuple
        sheet_uuid: Required predetermined UUID for the sheet

    Returns:
        list: Nested list representing the sheet s-expression
    """

    sheet_sexp = [
        "sheet",
        ["at", float(position[0]), float(position[1])],
        ["size", float(size[0]), float(size[1])],
        ["exclude_from_sim", "no"],
        ["in_bom", "yes"],
        ["on_board", "yes"],
        ["dnp", "no"],
        ["fields_autoplaced", "yes"],
        ["stroke", ["width", 0.1524], ["type", "solid"]],
        ["fill", ["color", 0, 0, 0, 0.0000]],
        ["uuid", sheet_uuid],
        ["property", "Sheetname", node_name,
            ["at", float(position[0]), float(position[1] - 0.7116), 0],
            ["effects", ["font", ["size", 1.27, 1.27]], ["justify", "left", "bottom"]]
        ],
        ["property", "Sheetfile", sheet_filename,
            ["at", float(position[0]), float(position[1] + size[1] + 0.5846), 0],
            ["effects", ["font", ["size", 1.27, 1.27]], ["justify", "left", "top"]]
        ]
    ]

    # TODO: Add hierarchical pins based on net connections

    return sheet_sexp


def preprocess_circuit(circuit, **options):
    """Add stuff to parts & nets for doing placement and routing of schematics."""

    def units(part):
        if len(part.unit) == 0:
            return [part]
        return part.unit.values()

    def initialize(part):
        """Initialize part or its part units."""

        pin_limit = options.get("orientation_pin_limit", 44)
        for part_unit in units(part):
            part_unit.tx = Tx.from_symtx(getattr(part_unit, "symtx", ""))

            num_pins = len(part_unit.pins)
            part_unit.orientation_locked = getattr(part_unit, "symtx", False) or not (
                1 < num_pins <= pin_limit
            )

            part_unit.grab_pins()

            for pin in part_unit:
                pin.pt = Point(pin.x * mils_per_mm, pin.y * mils_per_mm)
                if isinstance(pin.orientation, (int, float)):
                    pin.orientation = {
                        0: "R",
                        90: "D",
                        180: "L",
                        270: "U",
                    }.get(pin.orientation, pin.orientation)
                pin.routed = False

    def rotate_power_pins(part):
        """Rotate a part based on the direction of its power pins."""

        if not getattr(part, "symtx", ""):
            return

        def is_pwr(net):
            return net_name.startswith("+")

        def is_gnd(net):
            return "gnd" in net_name.lower()

        dont_rotate_pin_cnt = options.get("dont_rotate_pin_count", 10000)

        for part_unit in units(part):
            if len(part_unit) > dont_rotate_pin_cnt:
                return

            rotation_tally = Counter()
            for pin in part_unit:
                net_name = getattr(pin.net, "name", "").lower()
                if is_gnd(net_name):
                    if pin.orientation == "U":
                        rotation_tally[0] += 1
                    if pin.orientation == "D":
                        rotation_tally[180] += 1
                    if pin.orientation == "L":
                        rotation_tally[90] += 1
                    if pin.orientation == "R":
                        rotation_tally[270] += 1
                elif is_pwr(net_name):
                    if pin.orientation == "D":
                        rotation_tally[0] += 1
                    if pin.orientation == "U":
                        rotation_tally[180] += 1
                    if pin.orientation == "L":
                        rotation_tally[270] += 1
                    if pin.orientation == "R":
                        rotation_tally[90] += 1

            try:
                rotation = rotation_tally.most_common()[0][0]
            except IndexError:
                pass
            else:
                tx_cw_90 = Tx(a=0, b=-1, c=1, d=0)
                for _ in range(int(round(rotation / 90))):
                    part_unit.tx = part_unit.tx * tx_cw_90

    def calc_part_bbox(part):
        """Calculate the labeled bounding boxes and store it in the part."""

        bare_bboxes = calc_symbol_bbox(part)[1:]

        for part_unit, bare_bbox in zip(units(part), bare_bboxes):
            resize_wh = Vector(0, 0)
            if bare_bbox.w < 100:
                resize_wh.x = (100 - bare_bbox.w) / 2
            if bare_bbox.h < 100:
                resize_wh.y = (100 - bare_bbox.h) / 2
            bare_bbox = bare_bbox.resize(resize_wh)

            part_unit.lbl_bbox = BBox()
            part_unit.lbl_bbox.add(bare_bbox)
            for pin in part_unit:
                if pin.stub:
                    hlbl_bbox = calc_hier_label_bbox(pin.net.name, pin.orientation)
                    hlbl_bbox *= Tx().move(pin.pt)
                    part_unit.lbl_bbox.add(hlbl_bbox)

            part_unit.bbox = part_unit.lbl_bbox

    for part in circuit.parts:
        initialize(part)
        rotate_power_pins(part)
        calc_part_bbox(part)


def finalize_parts_and_nets(circuit, **options):
    """Restore parts and nets after place & route is done."""

    net_terminals = (p for p in circuit.parts if isinstance(p, NetTerminal))
    circuit.rmv_parts(*net_terminals)

    for part in circuit.parts:
        part.grab_pins()

    rmv_attr(circuit.parts, ("force", "bbox", "lbl_bbox", "tx"))


def group_parts_by_hierarchy(circuit):
    """Group circuit parts by their complete hierarchical paths.

    Args:
        circuit: SKiDL Circuit object

    Returns:
        dict: Dictionary mapping hierarchical paths to lists of parts
              Key format: "level1" or "level1/level2" or "level1/level2/level3" etc.
    """
    hierarchy_groups = {}

    for part in circuit.parts:
        if not isinstance(part, NetTerminal) and hasattr(part, 'hiertuple'):
            # Get the hierarchical path (skip root empty string)
            path_components = [level for level in part.hiertuple[1:] if level]

            if path_components:
                # Ensure parent levels exist even when they contain no parts.
                for i in range(1, len(path_components) + 1):
                    level_path = "/".join(path_components[:i])
                    hierarchy_groups.setdefault(level_path, [])

                # Create path string like "power" or "power/regulators" or "power/regulators/ldo".
                node_path = "/".join(path_components)
            else:
                # Root level part
                node_path = ""

            if node_path not in hierarchy_groups:
                hierarchy_groups[node_path] = []
            hierarchy_groups[node_path].append(part)

    return hierarchy_groups


def build_node_map(node, path="", node_map=None):
    """Build a lookup of hierarchy paths to SchNode instances."""

    if node_map is None:
        node_map = {}

    node_map[path] = node
    for name, child in node.children.items():
        child_path = f"{path}/{name}" if path else name
        build_node_map(child, child_path, node_map)

    return node_map


def get_all_hierarchy_levels(hierarchy_groups):
    """Extract all unique hierarchy levels from the grouped parts.

    Args:
        hierarchy_groups: Dict from group_parts_by_hierarchy

    Returns:
        set: All unique hierarchy levels (e.g., {"power", "power/regulators", "analog"})
    """
    all_levels = set()

    for path in hierarchy_groups.keys():
        if path:  # Skip root level
            # Add this level and all parent levels
            components = path.split("/")
            for i in range(1, len(components) + 1):
                level_path = "/".join(components[:i])
                all_levels.add(level_path)

    return all_levels


def create_schematic_sexp_for_hierarchy_group(parts_group, title="SKiDL-Generated Schematic", main_sheet_uuid=None, sheet_uuids=None, sheet_tx=None, **options):
    """Create schematic s-expression for a specific hierarchy group.

    Args:
        parts_group: List of parts in this hierarchy level
        title: Title for the schematic
        main_sheet_uuid: UUID of the main/root schematic sheet
        sheet_uuids: Dict mapping subcircuit names to their sheet UUIDs
        options: Generation options

    Returns:
        Sexp: S-expression object representing the schematic for this group
    """
    # Generate unique UUID for schematic
    sch_uuid = str(uuid.uuid4())

    if sheet_uuids is None:
        sheet_uuids = {}

    # Collect unique library symbols used in this group
    unique_lib_parts = {}  # Map lib_id -> part (to avoid duplicates)
    symbol_parts = []  # Store parts and their data for later processing

    sheet_tx = sheet_tx or Tx()
    for i, part in enumerate(parts_group):
        if not hasattr(part, "tx"):
            grid_x = (i % 5) * 25.4 * mils_per_mm  # 5 parts per row, 1 inch spacing
            grid_y = (i // 5) * 12.7 * mils_per_mm  # 0.5 inch row spacing
            part.tx = Tx().move(Point(grid_x, grid_y))

        # Get library and part name
        lib_name = os.path.splitext(part.lib.filename)[0] if hasattr(part.lib, 'filename') and part.lib.filename else "Device"
        part_name = part.name or "Unknown"
        lib_id = f"{lib_name}:{part_name}"

        # Store unique part for lib_symbols (only one definition per lib_id needed)
        if lib_id not in unique_lib_parts:
            unique_lib_parts[lib_id] = part

        symbol_parts.append((part, sheet_tx))

    # Create lib_symbols section using actual SKiDL part data
    lib_symbols_list = ["lib_symbols"]
    seen_symbols = set()
    for lib_id, part in unique_lib_parts.items():
        for symbol_def in get_lib_symbol_definition_from_part(part):
            symbol_name = symbol_def[1]
            if symbol_name in seen_symbols:
                continue
            lib_symbols_list.append(symbol_def)
            seen_symbols.add(symbol_name)

    # Create basic schematic structure
    schematic_list = [
        "kicad_sch",
        ["version", 20230409],
        ["generator", "SKiDL"],
        ["generator_version", get_skidl_version()],
        ["uuid", sch_uuid],
        ["paper", "A4"],
        create_title_block_sexp(title),
        lib_symbols_list  # Now contains actual symbol definitions
    ]

    # Add symbol instances for each part
    # Use main_sheet_uuid if provided, otherwise fall back to this schematic's UUID
    instance_main_uuid = main_sheet_uuid if main_sheet_uuid else sch_uuid
    for part, tx in symbol_parts:
        symbol_sexp = part_to_symbol_sexp(part, tx, instance_main_uuid, sheet_uuids)
        if symbol_sexp:
            schematic_list.append(symbol_sexp)

    # Add basic wiring if requested
    if options.get("add_wires", False):
        # This is a placeholder - would need proper routing logic
        for net in circuit.nets:
            if len(net.pins) >= 2:
                # Simple wire between first two pins
                wire_list = [
                    "wire",
                    ["pts",
                        ["xy", 100, 100],
                        ["xy", 125, 100]
                    ],
                    ["stroke", ["width", 0], ["type", "default"]],
                    ["uuid", str(uuid.uuid4())]
                ]
                schematic_list.append(wire_list)

    return Sexp(schematic_list)


def create_subcircuit_schematic_with_child_sheets(parts_group, current_path, title, main_sheet_uuid, sheet_uuids, hierarchy_groups, top_name, node_map=None, sheet_tx=None, **options):
    """Create schematic s-expression for a hierarchy level with child sheet references.

    Args:
        parts_group: List of parts at this hierarchy level
        current_path: Current hierarchy path (e.g., "power" or "power/regulators")
        title: Title for the schematic
        main_sheet_uuid: UUID of the main/root schematic sheet
        sheet_uuids: Dict mapping hierarchy paths to their sheet UUIDs
        hierarchy_groups: All hierarchy groups for finding children
        top_name: Name prefix for child sheet files
        options: Generation options

    Returns:
        Sexp: S-expression object representing the schematic for this level
    """
    # Use the predetermined UUID for this schematic level (not a random one)
    sch_uuid = sheet_uuids.get(current_path) if current_path and sheet_uuids else str(uuid.uuid4())

    if sheet_uuids is None:
        sheet_uuids = {}

    sheet_tx = sheet_tx or Tx()

    # Collect unique library symbols used in this group
    unique_lib_parts = {}  # Map lib_id -> part (to avoid duplicates)
    symbol_parts = []  # Store parts and their data for later processing

    for i, part in enumerate(parts_group):
        if not hasattr(part, "tx"):
            grid_x = (i % 5) * 25.4 * mils_per_mm  # 5 parts per row, 1 inch spacing
            grid_y = (i // 5) * 12.7 * mils_per_mm  # 0.5 inch row spacing
            part.tx = Tx().move(Point(grid_x, grid_y))

        # Get library and part name
        lib_name = os.path.splitext(part.lib.filename)[0] if hasattr(part.lib, 'filename') and part.lib.filename else "Device"
        part_name = part.name or "Unknown"
        lib_id = f"{lib_name}:{part_name}"

        # Store unique part for lib_symbols (only one definition per lib_id needed)
        if lib_id not in unique_lib_parts:
            unique_lib_parts[lib_id] = part

        symbol_parts.append((part, sheet_tx))

    # Create lib_symbols section using actual SKiDL part data
    lib_symbols_list = ["lib_symbols"]
    seen_symbols = set()
    for lib_id, part in unique_lib_parts.items():
        for symbol_def in get_lib_symbol_definition_from_part(part):
            symbol_name = symbol_def[1]
            if symbol_name in seen_symbols:
                continue
            lib_symbols_list.append(symbol_def)
            seen_symbols.add(symbol_name)

    # Create basic schematic structure
    schematic_list = [
        "kicad_sch",
        ["version", 20230409],
        ["generator", "SKiDL"],
        ["generator_version", get_skidl_version()],
        ["uuid", sch_uuid],
        ["paper", "A4"],
        create_title_block_sexp(title),
        lib_symbols_list  # Now contains actual symbol definitions
    ]

    # Add symbol instances for each part
    # Always use the main sheet UUID as root - let part_to_symbol_sexp build the full hierarchical path
    for part, tx in symbol_parts:
        symbol_sexp = part_to_symbol_sexp(part, tx, main_sheet_uuid, sheet_uuids)
        if symbol_sexp:
            schematic_list.append(symbol_sexp)

    if node_map:
        node = node_map.get(current_path)
        if node:
            for net, segments in node.wires.items():
                for wire in net_to_wire_sexp(net, segments, sheet_tx):
                    schematic_list.append(wire)
            for part in node.parts:
                for pin in part:
                    label = pin_label_to_sexp(pin, sheet_tx)
                    if label:
                        schematic_list.append(label)

    # Now add child sheet symbols for any deeper levels
    child_levels = []
    current_depth = len(current_path.split("/"))

    # Find all immediate child levels
    for path in hierarchy_groups.keys():
        if path.startswith(current_path + "/") and path != current_path:
            # Check if this is an immediate child (one level deeper)
            path_components = path.split("/")
            if len(path_components) == current_depth + 1:
                child_name = path_components[-1]  # Last component is the child name
                child_levels.append((child_name, path))

    if child_levels:
        # Add hierarchical sheet symbols for child levels
        # Find a good position for child sheets (after any existing content)
        sheet_y = 50.0 + len(parts_group) * 12.7  # Position below parts

        for i, (child_name, child_path) in enumerate(child_levels):
            # Create sheet symbol for child
            safe_child_name = child_path.replace("/", "_")
            sheet_filename = f"{top_name}_{safe_child_name}.kicad_sch"
            sheet_position = (50.0, sheet_y + i * 30.0)
            sheet_size = (25.4, 20.0)  # 1 inch wide, 0.8 inch tall

            # Get the predetermined UUID for this child sheet
            child_sheet_uuid = sheet_uuids.get(child_path)
            sheet_sexp = create_hierarchical_sheet_sexp(
                child_name, sheet_filename, sheet_position, sheet_size, child_sheet_uuid
            )
            schematic_list.append(sheet_sexp)

    return Sexp(schematic_list)


def create_main_schematic_sexp(circuit, title, hierarchy_groups, top_name, node_map=None, sheet_tx=None, **options):
    """Create the main schematic s-expression, potentially with hierarchical sheets.

    Args:
        circuit: SKiDL Circuit object
        title: Title for the schematic
        hierarchy_groups: Dictionary of hierarchy groups
        top_name: Name prefix for subsheet files
        options: Generation options

    Returns:
        tuple: (Sexp object for the main schematic, main sheet UUID, dict of sheet UUIDs)
    """
    sheet_tx = sheet_tx or Tx()

    # Generate stable UUID for main schematic using same approach as netlist generation
    # For root level, netlist generation uses "/" path, so we generate UUID from that
    main_sheet_uuid = str(uuid.uuid5(namespace_uuid, "/"))

    # Generate sheet UUIDs for ALL hierarchy levels (recursive)
    all_levels = get_all_hierarchy_levels(hierarchy_groups)
    sheet_uuids = {}

    for level_path in all_levels:
        # Generate UUID using same approach as netlist generation
        # Convert level_path like "power/regulators" to hierarchy tuple ("", "power", "regulators")
        path_components = level_path.split("/")
        hierarchy_tuple = ("",) + tuple(path_components)

        # Use netlist generation approach: generate UUID from the final level name
        sheet_uuids[level_path] = str(uuid.uuid5(namespace_uuid, path_components[-1]))

    # Collect lib_symbols needed for root level parts
    unique_lib_parts = {}
    if "" in hierarchy_groups:
        for part in hierarchy_groups[""]:
            lib_name = os.path.splitext(part.lib.filename)[0] if hasattr(part.lib, 'filename') and part.lib.filename else "Device"
            part_name = part.name or "Unknown"
            lib_id = f"{lib_name}:{part_name}"
            if lib_id not in unique_lib_parts:
                unique_lib_parts[lib_id] = part

    # Create lib_symbols section
    lib_symbols_list = ["lib_symbols"]
    seen_symbols = set()
    for lib_id, part in unique_lib_parts.items():
        for symbol_def in get_lib_symbol_definition_from_part(part):
            symbol_name = symbol_def[1]
            if symbol_name in seen_symbols:
                continue
            lib_symbols_list.append(symbol_def)
            seen_symbols.add(symbol_name)

    # Create main schematic structure
    schematic_list = [
        "kicad_sch",
        ["version", 20230409],
        ["generator", "SKiDL"],
        ["generator_version", get_skidl_version()],
        ["uuid", main_sheet_uuid],
        ["paper", "A4"],
        create_title_block_sexp(title),
        lib_symbols_list
    ]

    # Add root level parts (if any) to main schematic
    if "" in hierarchy_groups:
        root_parts = hierarchy_groups[""]
        for i, part in enumerate(root_parts):
            if not hasattr(part, "tx"):
                grid_x = (i % 5) * 25.4 * mils_per_mm
                grid_y = (i // 5) * 12.7 * mils_per_mm
                part.tx = Tx().move(Point(grid_x, grid_y))

            symbol_sexp = part_to_symbol_sexp(part, sheet_tx, main_sheet_uuid, sheet_uuids)
            if symbol_sexp:
                schematic_list.append(symbol_sexp)

    if node_map:
        node = node_map.get("")
        if node:
            for net, segments in node.wires.items():
                for wire in net_to_wire_sexp(net, segments, sheet_tx):
                    schematic_list.append(wire)
            for part in node.parts:
                for pin in part:
                    label = pin_label_to_sexp(pin, sheet_tx)
                    if label:
                        schematic_list.append(label)

    # Add hierarchical sheet symbols for immediate child subcircuits only (top-level)
    sheet_y = 50.0  # Start position for sheets
    top_level_subcircuits = set()

    # Find all top-level subcircuits (those without '/' in their path)
    for node_path in hierarchy_groups.keys():
        if node_path and "/" not in node_path:  # Top-level subcircuit
            top_level_subcircuits.add(node_path)

    for i, node_name in enumerate(sorted(top_level_subcircuits)):
        # Create sheet symbol
        sheet_filename = f"{top_name}_{node_name}.kicad_sch"
        sheet_position = (50.0, sheet_y + i * 30.0)
        sheet_size = (25.4, 20.0)  # 1 inch wide, 0.8 inch tall

        # Get the predetermined UUID for this top-level sheet
        top_level_sheet_uuid = sheet_uuids.get(node_name)
        sheet_sexp = create_hierarchical_sheet_sexp(
            node_name, sheet_filename, sheet_position, sheet_size, top_level_sheet_uuid
        )
        schematic_list.append(sheet_sexp)

    return Sexp(schematic_list), main_sheet_uuid, sheet_uuids


@export_to_all
def gen_schematic(
    circuit,
    filepath=".",
    top_name=get_script_name(),
    title="SKiDL-Generated Schematic",
    flatness=0.0,
    retries=2,
    **options
):
    """Create a schematic file from a Circuit object using s-expressions.

    Args:
        circuit (Circuit): The Circuit object that will have a schematic generated for it.
        filepath (str, optional): The directory where the schematic files are placed. Defaults to ".".
        top_name (str, optional): The name for the top of the circuit hierarchy. Defaults to get_script_name().
        title (str, optional): The title of the schematic. Defaults to "SKiDL-Generated Schematic".
        flatness (float, optional): Currently unused in KiCad9 implementation.
        retries (int, optional): Number of times to re-try if routing fails. Defaults to 2.
        options (dict, optional): Dict of options and values, usually for drawing/debugging.
    """

    from skidl.logger import active_logger
    from skidl import KICAD9
    from skidl.schematics.place import PlacementFailure
    from skidl.schematics.route import RoutingFailure
    from skidl.tools import tool_modules
    from skidl.schematics.sch_node import SchNode

    def need_quote(x):
        key = x[0] if x else None
        if key in (
            "title",
            "date",
            "company",
            "comment",
            "path",
            "project",
            "property",
            "name",
            "number",
            "lib_id",
            "reference",
            "label",
            "hierarchical_label",
            "global_label",
        ):
            return True
        return False

    def need_quote_alternate(x):
        key = x[0] if x else None
        if key == "alternate":
            return True
        return False

    try:
        main_schematic_filename = os.path.join(filepath, f"{top_name}.kicad_sch")

        if not circuit.parts:
            active_logger.warning("Circuit has no parts to generate schematic from.")
            return

        active_logger.info(f"Generating KiCad 9 schematic: {main_schematic_filename}")
        active_logger.info(f"Processing {len(circuit.parts)} parts and {len(circuit.nets)} nets")

        hierarchy_groups = group_parts_by_hierarchy(circuit)
        active_logger.info(f"Found {len(hierarchy_groups)} hierarchy levels: {list(hierarchy_groups.keys())}")

        options["use_push_pull"] = True
        options["rotate_parts"] = True
        options["pt_to_pt_mult"] = 5
        options["pin_normalize"] = True

        expansion_factor = 1.0
        failure_type = None

        for _ in range(retries):
            preprocess_circuit(circuit, **options)
            node = SchNode(circuit, tool_modules[KICAD9], filepath, top_name, title, flatness)

            try:
                node.place(tool=KICAD9, expansion_factor=expansion_factor, **options)
                node.route(tool=KICAD9, **options)
            except PlacementFailure as e:
                finalize_parts_and_nets(circuit, **options)
                failure_type = e
                continue
            except RoutingFailure as e:
                finalize_parts_and_nets(circuit, **options)
                expansion_factor *= 1.5
                failure_type = e
                continue

            node_map = build_node_map(node)
            sheet_tx = Tx(a=mms_per_mil, d=mms_per_mil)

            main_schematic_sexp, main_sheet_uuid, sheet_uuids = create_main_schematic_sexp(
                circuit,
                title,
                hierarchy_groups,
                top_name,
                node_map=node_map,
                sheet_tx=sheet_tx,
                **options,
            )

            main_schematic_sexp.add_quotes(need_quote)
            main_schematic_sexp.add_quotes(need_quote_alternate, stop_idx=2)

            with open(main_schematic_filename, "w") as f:
                f.write(main_schematic_sexp.to_str())

            active_logger.info(f"Main schematic created: {main_schematic_filename}")

            subcircuit_count = 0

            def create_subcircuit_schematic(node_path, parts_list, depth=0):
                nonlocal subcircuit_count

                safe_name = node_path.replace("/", "_")
                subcircuit_filename = os.path.join(filepath, f"{top_name}_{safe_name}.kicad_sch")
                subcircuit_title = f"{title} - {node_path}"

                active_logger.info(f"{'  ' * depth}Creating schematic for level: {node_path}")

                subcircuit_sexp = create_subcircuit_schematic_with_child_sheets(
                    parts_list,
                    node_path,
                    subcircuit_title,
                    main_sheet_uuid,
                    sheet_uuids,
                    hierarchy_groups,
                    top_name,
                    node_map=node_map,
                    sheet_tx=sheet_tx,
                    **options,
                )

                subcircuit_sexp.add_quotes(need_quote)
                subcircuit_sexp.add_quotes(need_quote_alternate, stop_idx=2)

                with open(subcircuit_filename, "w") as f:
                    f.write(subcircuit_sexp.to_str())

                active_logger.info(f"{'  ' * depth}Subcircuit schematic created: {subcircuit_filename}")
                return 1

            for node_path, parts in hierarchy_groups.items():
                if node_path == "":
                    continue

                count = create_subcircuit_schematic(node_path, parts)
                subcircuit_count += count

            if subcircuit_count > 0:
                active_logger.info(f"Generated {subcircuit_count} subcircuit schematic files")
            else:
                active_logger.info("No subcircuits found - generated single flat schematic")

            if options.get("collect_stats"):
                stats = node.collect_stats(**options)
                with open(options["stats_file"], "a") as f:
                    f.write(stats)

            finalize_parts_and_nets(circuit, **options)
            return

        if options.get("collect_stats"):
            stats = "-1\n"
            with open(options["stats_file"], "a") as f:
                f.write(stats)

        finalize_parts_and_nets(circuit, **options)
        raise failure_type

    except Exception as e:
        active_logger.error(f"Error generating KiCad 9 schematic: {str(e)}")
        raise
