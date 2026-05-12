"""Operator tooling for ha-switchbee.

`tools/` holds standalone CLI helpers (migration, probes, fixture sanitizers)
that are NOT loaded by Home Assistant at runtime. They are pure Python and
must not `import homeassistant.*` at module import time.
"""
