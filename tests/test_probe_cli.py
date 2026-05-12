"""Argparse-level tests for `tools/probe.py`.

These tests do NOT spin up the fake CU. They only exercise the CLI surface:
help text, required flag enforcement, alias acceptance, and the
`--observe-seconds=0` short-circuit (config fetch only, no observation loop).

The full end-to-end run against the fake CU lives in
`tests/test_probe_against_fake_cu.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROBE_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "probe.py"


def _run_probe(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PROBE_SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_help_exits_zero_and_lists_required_flags() -> None:
    result = _run_probe("--help")
    assert result.returncode == 0, result.stderr
    for flag in (
        "--host",
        "--username",
        "--password",
        "--duration",
        "--read-only",
    ):
        assert flag in result.stdout, f"missing {flag} in --help"
    # Env-var pattern documented in --help (operator runs probe with
    # `SB_USER=... SB_PASS=... python tools/probe.py ...`).
    assert "SB_USER" in result.stdout or "SB_PASS" in result.stdout


def test_help_lists_prompt_aliases() -> None:
    """The prompt specifies `--user`, `--pass`, `--observe-seconds`. The plan
    uses `--username`, `--password`, `--duration`. Both must work."""
    result = _run_probe("--help")
    assert result.returncode == 0, result.stderr
    for alias in ("--user", "--pass", "--observe-seconds", "--port"):
        assert alias in result.stdout, f"missing alias {alias} in --help"


def test_missing_host_exits_nonzero() -> None:
    result = _run_probe(
        "--username",
        "u",
        "--password",
        "p",
        "--read-only",
        "--no-banner",
        "--duration",
        "0",
    )
    # argparse exits 2 on missing required arg.
    assert result.returncode != 0
    assert "host" in (result.stderr + result.stdout).lower()


def test_live_mode_requires_read_only_flag() -> None:
    """Without `--read-only`, the probe must refuse to run against a host.

    Spirit of the rule: this script cannot mutate anything on the CU. The
    simplest enforcement: insist the operator pass `--read-only` explicitly
    whenever they target a host. Tests can pass `--read-only` freely.
    """
    result = _run_probe(
        "--host",
        "127.0.0.1",
        "--username",
        "u",
        "--password",
        "p",
        "--duration",
        "0",
        "--no-banner",
    )
    assert result.returncode != 0, "probe must refuse to run without --read-only"
    combined = (result.stderr + result.stdout).lower()
    assert "read-only" in combined or "read_only" in combined


def test_observe_seconds_zero_is_accepted_in_parser() -> None:
    """Argparse must allow `--observe-seconds 0` / `--duration 0` (config fetch only).

    This test checks the parser without spinning up the protocol stack: we
    import the module and call `build_parser()` directly. Connection-refused
    behavior with a real --read-only run is covered in the fake-CU test.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_probe_under_test", PROBE_SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    parser = module.build_parser()
    namespace = parser.parse_args(
        [
            "--host",
            "127.0.0.1",
            "--username",
            "u",
            "--password",
            "p",
            "--duration",
            "0",
            "--read-only",
            "--no-banner",
        ]
    )
    assert namespace.duration == 0
    assert namespace.read_only is True
    assert namespace.host == "127.0.0.1"


def test_aliases_resolve_to_same_namespace_attrs() -> None:
    """`--user` -> `username`, `--pass` -> `password`, `--observe-seconds` -> `duration`."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_probe_under_test_aliases", PROBE_SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    parser = module.build_parser()
    namespace = parser.parse_args(
        [
            "--host",
            "127.0.0.1",
            "--user",
            "u",
            "--pass",
            "p",
            "--observe-seconds",
            "2",
            "--read-only",
            "--no-banner",
        ]
    )
    assert namespace.username == "u"
    assert namespace.password == "p"
    assert namespace.duration == 2


def test_probe_module_never_calls_operate() -> None:
    """Static guard: probe.py source must not reference `.operate(` at all.

    The plan's read-only invariant is enforced structurally: the script
    simply does not contain any OPERATE call site. Easiest invariant to
    audit, easiest to violate by accident, so we lock it with a test.
    """
    source = PROBE_SCRIPT.read_text(encoding="utf-8")
    assert ".operate(" not in source, (
        "probe.py must never call client.operate() — read-only invariant"
    )
