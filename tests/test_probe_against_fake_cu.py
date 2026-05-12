"""End-to-end test for `tools/probe.py` against the in-process fake CU.

Spins up the fake CU on an ephemeral port, runs `python tools/probe.py
--host 127.0.0.1 --port <ephemeral> --read-only --duration <small>` as
a subprocess, and asserts:

* exit code 0
* stdout contains the expected CU summary fields (normalized mac, name,
  version, type counts)
* push events observed during the window are logged with the stable
  `[push] item_id=N name=... new_value=...` shape

The probe must not touch any real CU. The plan's Phase 6 live runs are
covered by a different (HITL) workflow.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fake_cu import (
    SAMPLE_MAC,
    SAMPLE_NAME,
    SAMPLE_VERSION,
    SAMPLE_ZONES,
    FakeCU,
)

PROBE_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "probe.py"


@pytest.fixture(autouse=True)
def _enable_sockets(socket_enabled):
    """Enable real socket I/O for every test in this module.

    The probe subprocess connects to 127.0.0.1 on the fake CU's ephemeral
    port. pytest_socket otherwise blocks that.
    """
    yield


def _run_probe_subprocess(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run probe.py and capture stdout/stderr with a hard timeout."""
    return subprocess.run(
        [sys.executable, str(PROBE_SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


async def test_probe_runs_end_to_end_against_fake_cu_with_duration_zero() -> None:
    """`--duration 0` exercises connect + LOGIN + GET_CONFIGURATION + clean stop.

    This is the canonical health-check form used in the plan:
        python tools/probe.py --host ... --read-only --duration 0
    """
    async with FakeCU() as cu:
        port = cu.port

        # Run the probe as a subprocess so we test the real argparse and
        # `if __name__ == "__main__"` path, not just `main()`.
        proc = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _run_probe_subprocess(
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--username",
                "user",
                "--password",
                "pass",
                "--duration",
                "0",
                "--read-only",
                "--no-banner",
            ),
        )

    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"

    stdout = proc.stdout
    # Normalized mac comes out as 12 lowercase hex.
    expected_normalized = SAMPLE_MAC.replace("-", "").replace(":", "").lower()
    assert expected_normalized in stdout
    assert SAMPLE_MAC in stdout
    assert SAMPLE_NAME in stdout
    assert SAMPLE_VERSION in stdout

    # Per-type breakdown: SAMPLE_ZONES has SWITCH=2, DIMMER=1, SHUTTER=1.
    type_counter: dict[str, int] = {}
    for zone in SAMPLE_ZONES:
        for item in zone["items"]:
            type_counter[item["type"]] = type_counter.get(item["type"], 0) + 1
    for type_name, count in type_counter.items():
        assert f"{type_name}={count}" in stdout, f"missing {type_name}={count} in stdout:\n{stdout}"

    # Summary line.
    assert "Probe complete" in stdout
    assert "Observed 0 push events" in stdout


async def test_probe_observes_push_events_during_window() -> None:
    """`--duration 2` lets the probe catch a CONFIGURATION_CHANGE the fake CU
    fires mid-window. Each push must be logged on its own line."""
    async with FakeCU() as cu:
        port = cu.port

        async def _fire_push_after_grace() -> None:
            # Give the subprocess time to start, LOGIN, and enter the
            # observation loop before we push the event.
            await asyncio.sleep(1.0)
            await cu.push_configuration_change(
                item_id=42,
                name="Spots Kitchen",
                value="ON",
            )

        loop = asyncio.get_running_loop()
        probe_future = loop.run_in_executor(
            None,
            lambda: _run_probe_subprocess(
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--username",
                "user",
                "--password",
                "pass",
                "--duration",
                "2",
                "--read-only",
                "--no-banner",
                timeout=20.0,
            ),
        )
        push_task = asyncio.create_task(_fire_push_after_grace())

        proc = await probe_future
        await push_task

    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    # Push log line — accept either single or double quotes around the name
    # (Python repr() may pick either, depending on content).
    assert "[push] item_id=42" in proc.stdout
    assert "Spots Kitchen" in proc.stdout
    assert "ON" in proc.stdout
    assert "Observed 1 push events" in proc.stdout
    assert "Probe complete" in proc.stdout
