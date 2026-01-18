# Running Tests

Requires Python 3.10+ (code uses `match` statements).

## Setup

```bash
pip install tox
```

Or use the venv:
```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt tox
```

## Running Tests

```bash
tox -e py311      # KICAD9, no SPICE tests
tox -e kicad9     # KICAD9 + SPICE tests
tox -e kicad8     # KICAD8
```

Other environments: `py{310,311,312,313}`, `kicad{5,6,7,8,9}`

## Notes

- 16 tests require `netlistsvg` (Node.js tool) and will fail without it
- `py*` envs default to `SKIDL_TOOL=KICAD9`
- `kicad9` env also sets `TEST_SPICE=1`
