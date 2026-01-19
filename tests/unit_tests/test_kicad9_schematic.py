# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

from pathlib import Path

import skidl
from simp_sexp import Sexp
from skidl import (
    Group,
    KICAD9,
    Net,
    Part,
    TEMPLATE,
    Circuit,
    generate_schematic,
    lib_search_paths,
    set_default_tool,
)
from skidl.schematics.place import PlacementFailure
from skidl.schematics.route import RoutingFailure


def _setup_kicad9_libs(tmp_path):
    set_default_tool(KICAD9)

    lib_dir = Path(__file__).resolve().parents[1] / "test_data" / "kicad9"
    lib_dir_str = str(lib_dir)
    if lib_dir_str not in lib_search_paths[KICAD9]:
        lib_search_paths[KICAD9].insert(0, lib_dir_str)

    orig_pickle_dir = skidl.config.pickle_dir
    tmp_pickle_dir = tmp_path / "lib_pickle_dir"
    tmp_pickle_dir.mkdir(parents=True, exist_ok=True)
    skidl.config.pickle_dir = str(tmp_pickle_dir)
    return orig_pickle_dir


def _find_wires(text):
    sexp = Sexp(text)
    return sexp.search("kicad_sch/wire", ignore_case=True)


def _wire_has_pts(wire):
    for item in wire:
        if isinstance(item, list) and item and item[0] == "pts":
            xy_pts = [pt for pt in item[1:] if isinstance(pt, list) and pt and pt[0] == "xy"]
            return len(xy_pts) >= 2
    return False


def _snap_point(x, y, digits=3):
    return (round(float(x), digits), round(float(y), digits))


def _collect_wire_endpoints(text):
    endpoints = set()
    for wire in _find_wires(text):
        for item in wire:
            if isinstance(item, list) and item and item[0] == "pts":
                for pt in item[1:]:
                    if isinstance(pt, list) and pt and pt[0] == "xy":
                        endpoints.add(_snap_point(pt[1], pt[2]))
    return endpoints


def _symbol_instances(sexp):
    instances = []
    for sym in sexp.search("kicad_sch/symbol", ignore_case=True):
        if not any(isinstance(item, list) and item and item[0] == "lib_id" for item in sym):
            continue

        lib_id = None
        at = None
        unit = 1
        mirror = None
        ref = None
        for item in sym:
            if isinstance(item, list) and item:
                if item[0] == "lib_id":
                    lib_id = item[1]
                elif item[0] == "at":
                    at = item
                elif item[0] == "unit":
                    unit = int(item[1])
                elif item[0] == "mirror":
                    mirror = item[1]
                elif item[0] == "property" and len(item) > 2 and item[1] == "Reference":
                    ref = item[2]

        if lib_id and at:
            instances.append(
                {
                    "lib_id": lib_id,
                    "x": float(at[1]),
                    "y": float(at[2]),
                    "angle": int(at[3]) if len(at) > 3 else 0,
                    "unit": unit,
                    "mirror": mirror,
                    "ref": ref,
                }
            )
    return instances


def _pins_from_symbol(sym):
    pins = []
    for item in sym:
        if not (isinstance(item, list) and item and item[0] == "pin"):
            continue
        pin_x = None
        pin_y = None
        pin_num = None
        for pin_item in item:
            if isinstance(pin_item, list) and pin_item and pin_item[0] == "at":
                pin_x, pin_y = pin_item[1:3]
            elif isinstance(pin_item, list) and pin_item and pin_item[0] == "number":
                pin_num = str(pin_item[1])
        if pin_x is not None and pin_y is not None and pin_num is not None:
            pins.append(
                {
                    "num": pin_num,
                    "x": float(pin_x),
                    "y": float(pin_y),
                }
            )
    return pins


def _lib_pin_map(sexp):
    lib_symbols = sexp.search("kicad_sch/lib_symbols", ignore_case=True)
    if not lib_symbols:
        return {}

    lib_map = {}
    lib_root = lib_symbols[0]
    for sym_def in lib_root[1:]:
        if not (isinstance(sym_def, list) and sym_def and sym_def[0] == "symbol"):
            continue
        lib_id = sym_def[1]
        part_name = lib_id.split(":")[-1]
        units = {}
        for item in sym_def[2:]:
            if isinstance(item, list) and item and item[0] == "symbol":
                unit_name = item[1]
                pins = _pins_from_symbol(item)
                if pins:
                    units[unit_name] = pins
        if not units:
            pins = _pins_from_symbol(sym_def)
            if pins:
                units[part_name] = pins
        lib_map[lib_id] = {"part_name": part_name, "units": units}
    return lib_map


def _rotate_point(x, y, angle):
    angle = angle % 360
    if angle == 0:
        return x, y
    if angle == 90:
        return -y, x
    if angle == 180:
        return -x, -y
    if angle == 270:
        return y, -x
    raise ValueError(f"Unsupported angle: {angle}")


def _pin_positions_for_instance(inst, lib_map):
    lib_info = lib_map.get(inst["lib_id"])
    if not lib_info:
        return {}

    part_name = lib_info["part_name"]
    unit_name = f"{part_name}_{inst['unit']}_{inst['unit']}"
    pins = lib_info["units"].get(unit_name)
    if pins is None:
        pins = next(iter(lib_info["units"].values()), [])

    positions = {}
    for pin in pins:
        x, y = _rotate_point(pin["x"], pin["y"], inst["angle"])
        if inst["mirror"] == "x":
            y = -y
        elif inst["mirror"] == "y":
            x = -x
        positions[pin["num"]] = _snap_point(x + inst["x"], y + inst["y"])
    return positions


def _find_labels(text):
    sexp = Sexp(text)
    labels = []
    for kind in ("label", "hierarchical_label", "global_label"):
        labels.extend(sexp.search(f"kicad_sch/{kind}", ignore_case=True))
    return labels


def _generate_schematic_with_seeds(seeds, **kwargs):
    last_exc = None
    for seed in seeds:
        try:
            generate_schematic(seed=seed, **kwargs)
            return
        except (RoutingFailure, PlacementFailure) as exc:
            last_exc = exc
    if last_exc:
        raise last_exc


def test_kicad9_schematic_wires_basic(tmp_path):
    orig_pickle_dir = _setup_kicad9_libs(tmp_path)
    try:
        default_circuit.reset()

        r = Part("Device", "R", dest=TEMPLATE)
        r1 = r()
        r2 = r()
        net = Net("NET")
        net += r1[1], r2[1]

        generate_schematic(
            filepath=str(tmp_path),
            top_name="wire_basic",
            flatness=1.0,
            retries=1,
            seed=1,
        )

        sch_path = tmp_path / "wire_basic.kicad_sch"
        text = sch_path.read_text()
        wires = _find_wires(text)
        assert wires
        assert any(_wire_has_pts(wire) for wire in wires)

        endpoints = _collect_wire_endpoints(text)
        assert endpoints

        sexp = Sexp(text)
        instances = _symbol_instances(sexp)
        assert instances
        lib_map = _lib_pin_map(sexp)
        assert lib_map

        for inst in instances:
            pin_positions = _pin_positions_for_instance(inst, lib_map)
            assert any(pos in endpoints for pos in pin_positions.values())
    finally:
        skidl.config.pickle_dir = orig_pickle_dir


def test_kicad9_schematic_file_path(tmp_path):
    orig_pickle_dir = _setup_kicad9_libs(tmp_path)
    try:
        default_circuit.reset()

        r = Part("Device", "R", dest=TEMPLATE)
        r1 = r()
        r2 = r()
        net = Net("NET")
        net += r1[1], r2[1]

        sch_path = tmp_path / "file_path.kicad_sch"
        generate_schematic(
            file_=str(sch_path),
            flatness=1.0,
            retries=1,
            seed=1,
        )

        assert sch_path.exists()
        text = sch_path.read_text()
        assert "kicad_sch" in text
    finally:
        skidl.config.pickle_dir = orig_pickle_dir


def test_kicad9_embedded_symbol_extends_resolved(tmp_path):
    orig_pickle_dir = _setup_kicad9_libs(tmp_path)
    try:
        default_circuit.reset()

        q1 = Part(
            "Transistor_BJT",
            "MMBT3904",
            ref="Q1",
            footprint="Package_TO_SOT_SMD:SOT-23",
        )
        q1["B"] += Net("BASE")
        q1["C"] += Net("COLL")
        q1["E"] += Net("GND")

        sch_path = tmp_path / "extends_test.kicad_sch"
        generate_schematic(
            file_=str(sch_path),
            flatness=1.0,
            retries=1,
            seed=1,
        )

        text = sch_path.read_text()
        sexp = Sexp(text)
        lib_symbols = sexp.search("kicad_sch/lib_symbols", ignore_case=True)
        assert lib_symbols

        lib_root = lib_symbols[0]
        target = None
        for item in lib_root[1:]:
            if isinstance(item, list) and item and item[0] == "symbol":
                if str(item[1]).endswith(":MMBT3904"):
                    target = item
                    break
        assert target
        assert not any(
            isinstance(item, list) and item and item[0] == "extends" for item in target
        )

        unit_names = [
            item[1]
            for item in target
            if isinstance(item, list) and item and item[0] == "symbol"
        ]
        assert "MMBT3904_0_1" in unit_names
        assert "MMBT3904_1_1" in unit_names

        unit = next(
            item
            for item in target
            if isinstance(item, list)
            and item
            and item[0] == "symbol"
            and item[1] == "MMBT3904_1_1"
        )
        pins = _pins_from_symbol(unit)
        pin_nums = {pin["num"] for pin in pins}
        assert {"1", "2", "3"}.issubset(pin_nums)

        assert not any(
            isinstance(item, list)
            and item
            and item[0] == "symbol"
            and item[1] == "Transistor_BJT:Q_NPN_BEC"
            for item in lib_root[1:]
        )
    finally:
        skidl.config.pickle_dir = orig_pickle_dir


def test_kicad9_multiunit_symbol_units_present(tmp_path):
    orig_pickle_dir = _setup_kicad9_libs(tmp_path)
    try:
        default_circuit.reset()

        vcc = Net("VCC")
        gnd = Net("GND")
        vout = Net("VOUT")

        u1 = Part(
            "Amplifier_Operational",
            "LM358",
            ref="U1",
            value="LM358",
            footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
        )

        u1[3] += vcc
        u1[2] += gnd
        u1[1] += vout
        u1[4] += gnd
        u1[8] += vcc

        sch_path = tmp_path / "multiunit.kicad_sch"
        generate_schematic(
            file_=str(sch_path),
            flatness=1.0,
            retries=1,
            seed=1,
        )

        sexp = Sexp(sch_path.read_text())
        instances = _symbol_instances(sexp)
        lm358_units = {
            inst["unit"]
            for inst in instances
            if inst["lib_id"].endswith(":LM358")
        }
        assert 1 in lm358_units
        assert 3 in lm358_units
    finally:
        skidl.config.pickle_dir = orig_pickle_dir


def test_kicad9_multiunit_symbol_not_at_origin(tmp_path):
    orig_pickle_dir = _setup_kicad9_libs(tmp_path)
    try:
        default_circuit.reset()

        vcc = Net("VCC")
        gnd = Net("GND")
        vin = Net("VIN")
        vout = Net("VOUT")
        inv_input = Net("INV_INPUT")

        r_in = Part(
            "Device",
            "R",
            ref="R1",
            value="10k",
            footprint="Resistor_SMD:R_0402_1005Metric",
        )
        r_fb = Part(
            "Device",
            "R",
            ref="R2",
            value="100k",
            footprint="Resistor_SMD:R_0402_1005Metric",
        )
        u1 = Part(
            "Amplifier_Operational",
            "LM358",
            ref="U1",
            value="LM358",
            footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
        )

        r_in[1] += vin
        r_in[2] += inv_input
        r_fb[1] += vout
        r_fb[2] += inv_input

        u1[1] += vout
        u1[2] += inv_input
        u1[3] += gnd
        u1[4] += gnd
        u1[8] += vcc

        sch_path = tmp_path / "multiunit_placement.kicad_sch"
        generate_schematic(
            file_=str(sch_path),
            flatness=1.0,
            retries=1,
            seed=1,
        )

        sexp = Sexp(sch_path.read_text())
        instances = _symbol_instances(sexp)
        lm358_by_unit = {
            inst["unit"]: inst
            for inst in instances
            if inst["lib_id"].endswith(":LM358")
        }
        assert 1 in lm358_by_unit
        assert 3 in lm358_by_unit

        for unit in (1, 3):
            inst = lm358_by_unit[unit]
            assert abs(inst["y"]) >= 1.0
    finally:
        skidl.config.pickle_dir = orig_pickle_dir


def test_kicad9_pin_orientation_mapping(tmp_path):
    orig_pickle_dir = _setup_kicad9_libs(tmp_path)
    kicad9_gen = None
    try:
        default_circuit.reset()

        r = Part("Device", "R", dest=TEMPLATE)
        r1 = r()
        vcc = Net("VCC")
        gnd = Net("GND")
        vcc += r1[1]
        gnd += r1[2]

        from skidl.tools.kicad9 import gen_schematic as kicad9_gen

        kicad9_gen.preprocess_circuit(default_circuit)

        pins = list(r1)
        assert pins
        pin_top = max(pins, key=lambda p: p.pt.y)
        pin_bottom = min(pins, key=lambda p: p.pt.y)
        assert pin_top.orientation == "D"
        assert pin_bottom.orientation == "U"
    finally:
        if kicad9_gen is not None:
            kicad9_gen.finalize_parts_and_nets(default_circuit)
        skidl.config.pickle_dir = orig_pickle_dir


def test_kicad9_schematic_wires_flat(tmp_path):
    orig_pickle_dir = _setup_kicad9_libs(tmp_path)
    try:
        default_circuit.reset()

        r = Part("Device", "R", dest=TEMPLATE)
        r1 = r()
        r2 = r()

        n1 = Net("N1")
        n2 = Net("N2")
        n1 += r1[1], r2[1]
        n2 += r1[2], r2[2]

        generate_schematic(
            filepath=str(tmp_path),
            top_name="wire_flat",
            flatness=1.0,
            retries=1,
            seed=0,
        )

        sch_path = tmp_path / "wire_flat.kicad_sch"
        text = sch_path.read_text()
        wires = _find_wires(text)
        assert wires
        assert any(_wire_has_pts(wire) for wire in wires)

        endpoints = _collect_wire_endpoints(text)
        sexp = Sexp(text)
        instances = _symbol_instances(sexp)
        lib_map = _lib_pin_map(sexp)
        assert instances
        assert lib_map

        for inst in instances:
            pin_positions = _pin_positions_for_instance(inst, lib_map)
            assert pin_positions
            for pos in pin_positions.values():
                assert pos in endpoints
    finally:
        skidl.config.pickle_dir = orig_pickle_dir


def test_kicad9_schematic_wires_hierarchy(tmp_path):
    orig_pickle_dir = _setup_kicad9_libs(tmp_path)
    try:
        default_circuit.reset()

        r = Part("Device", "R", dest=TEMPLATE)

        with Group("A"):
            r1 = r()
            r2 = r()

            n1 = Net("N1")
            n2 = Net("N2")
            n1 += r1[1], r2[1]
            n2 += r1[2], r2[2]

        _generate_schematic_with_seeds(
            seeds=(1, 2, 3, 4, 5),
            filepath=str(tmp_path),
            top_name="wire_hier",
            flatness=0.0,
            retries=1,
        )

        sub_files = [
            path
            for path in tmp_path.glob("wire_hier_*.kicad_sch")
            if path.name != "wire_hier.kicad_sch"
        ]
        assert sub_files
        sub_path = sub_files[0]
        sub_text = sub_path.read_text()
        wires = _find_wires(sub_text)
        assert wires
        assert any(_wire_has_pts(wire) for wire in wires)
    finally:
        skidl.config.pickle_dir = orig_pickle_dir


def test_kicad9_schematic_labels_single_pin_net(tmp_path):
    orig_pickle_dir = _setup_kicad9_libs(tmp_path)
    try:
        default_circuit.reset()

        r = Part("Device", "R", dest=TEMPLATE)
        r1 = r()
        vcc = Net("VCC")
        vcc += r1[1]

        generate_schematic(
            filepath=str(tmp_path),
            top_name="label_single_pin",
            flatness=1.0,
            retries=1,
            seed=3,
        )

        sch_path = tmp_path / "label_single_pin.kicad_sch"
        text = sch_path.read_text()
        labels = _find_labels(text)
        assert any(lbl[0] == "global_label" and lbl[1] == "VCC" for lbl in labels)
    finally:
        skidl.config.pickle_dir = orig_pickle_dir


def test_kicad9_schematic_netterminal_named_circuit(tmp_path):
    orig_pickle_dir = _setup_kicad9_libs(tmp_path)
    try:
        default_circuit.reset()

        ckt = Circuit(name="NetTerminal_Circuit")
        with ckt:
            r = Part("Device", "R", dest=TEMPLATE)
            with Group("A"):
                r1 = r()
            with Group("B"):
                r2 = r()
            net_ab = Net("NET_AB")
            net_ab += r1[1], r2[1]

        ckt.generate_schematic(
            file_=str(tmp_path),
            tool=KICAD9,
            top_name="netterminal_named",
            flatness=0.0,
            retries=1,
            seed=2,
        )

        sch_path = tmp_path / "netterminal_named.kicad_sch"
        assert sch_path.exists()
    finally:
        skidl.config.pickle_dir = orig_pickle_dir
