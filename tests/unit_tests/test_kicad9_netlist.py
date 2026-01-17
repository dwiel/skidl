# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

import re
import uuid
from pathlib import Path

import skidl
from skidl import Group, Net, Part, TEMPLATE, generate_netlist, generate_schematic
from skidl import KICAD9, lib_search_paths, set_default_tool
from skidl.tools.kicad9.gen_netlist import namespace_uuid


def test_kicad9_hier_sheetpath_tstamps_match_schematic(tmp_path):
    set_default_tool(KICAD9)

    lib_dir = Path(__file__).resolve().parents[1] / "test_data" / "kicad9"
    lib_dir_str = str(lib_dir)
    if lib_dir_str not in lib_search_paths[KICAD9]:
        lib_search_paths[KICAD9].insert(0, lib_dir_str)

    orig_pickle_dir = skidl.config.pickle_dir
    tmp_pickle_dir = tmp_path / "lib_pickle_dir"
    tmp_pickle_dir.mkdir(parents=True, exist_ok=True)
    skidl.config.pickle_dir = str(tmp_pickle_dir)

    try:
        r = Part("Device", "R", dest=TEMPLATE)

        with Group("A"):
            r1 = r()
            r2 = r()

        Net("N1") & r1[1]
        Net("N2") & r1[2]
        Net("N3") & r2[1]
        Net("N4") & r2[2]

        netlist = generate_netlist()
        generate_schematic(filepath=str(tmp_path), top_name="top")

        match = re.search(r'\(comp\s+.*?\(ref "R1"\).*?\(sheetpath\s+\(names "[^"]*"\)\s+\(tstamps "([^"]+)"\)\)', netlist, re.DOTALL)
        assert match is not None
        sheetpath_tstamp = match.group(1)

        root_uuid = str(uuid.uuid5(namespace_uuid, "/"))
        hier_levels = [level for level in r1.hiertuple[1:] if level]
        expected_path = [root_uuid] + [str(uuid.uuid5(namespace_uuid, level)) for level in hier_levels]
        expected_tstamp = "/" + "/".join(expected_path) + "/"

        assert sheetpath_tstamp == expected_tstamp

        node_path = "/".join(hier_levels)
        safe_name = node_path.replace("/", "_")
        sub_path = tmp_path / f"top_{safe_name}.kicad_sch"
        assert sub_path.exists()
        sub_text = sub_path.read_text()
        expected_instance = f'(path "/{"/".join(expected_path)}"'
        assert expected_instance in sub_text
    finally:
        skidl.config.pickle_dir = orig_pickle_dir
