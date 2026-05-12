#!/usr/bin/env python3
"""ha-switchbee read-only protocol probe.

Standalone CLI that connects to a SwitchBee Central Unit over its local
WebSocket protocol, performs LOGIN, fetches GET_CONFIGURATION, observes
push notifications for a configurable window, and exits cleanly. Pairs
with `tests/fake_cu.py` for offline integration testing and with the
real CU at `192.168.68.57` for Phase 6 live validation.

Canonical invocation (per implementation plan, Phase 6):

    python tools/probe.py \\
        --host 192.168.68.57 \\
        --username "$SB_USER" \\
        --password "$SB_PASS" \\
        --read-only \\
        --duration 60

Aliases accepted (prompt-side names):

    --user / --pass / --observe-seconds

Behavior:

1. Print a coexistence banner and sleep 5s before connecting, so the
   operator can Ctrl-C if the CU is in use by the homebridge bridge.
   Skip with `--no-banner` (intended for tests and dry-runs).
2. Connect to ws://{host}:{port}, LOGIN, GET_CONFIGURATION.
3. Pretty-print CU summary: raw + normalized MAC, name, version,
   zone/item counts, per-`type` breakdown.
4. Listen for CONFIGURATION_CHANGE pushes for `--duration` seconds.
5. Disconnect cleanly. Exit 0 on success, non-zero on any failure.

Read-only invariant:

The probe must never mutate state on the CU. Enforcement is structural:
this file does not contain any OPERATE call site, and the operator must
pass `--read-only` explicitly when targeting a real host. A unit test
in `tests/test_probe_cli.py` asserts the source-level guard.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import time
from collections import Counter
from pathlib import Path

# Allow `python tools/probe.py ...` to import the integration package
# without `pip install -e .`. The pytest path already injects the project
# root, but the subprocess path used by the CLI tests does not.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import aiohttp  # noqa: E402

from custom_components.ha_switchbee.switchbee_ws import (  # noqa: E402
    CUMACMissingError,
    PushEvent,
    SwitchBeeProtocolError,
    SwitchBeeWSClient,
    normalize_cu_mac,
)

DEFAULT_PORT = 7891
DEFAULT_DURATION_SECONDS = 60
BANNER_GRACE_SECONDS = 5

_BANNER = (
    "NOTE: this probe opens one WebSocket connection to the CU. If the CU "
    "does not accept concurrent WS clients, this may briefly disconnect the "
    "existing homebridge-switchbee bridge. The bridge will self-recover "
    "within ~2 min via the switchbee-watchdog cron on the Ginnie PC. "
    f"Press Ctrl-C within {BANNER_GRACE_SECONDS}s to abort."
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Public so tests can introspect the flag set without spawning a
    subprocess. Both plan-canonical flags (`--username`, `--password`,
    `--duration`) and prompt aliases (`--user`, `--pass`,
    `--observe-seconds`) resolve to the same namespace attributes.
    Env-var fallback: `SB_USER` / `SB_PASS` are honored when the
    matching flag is omitted; explicit flags always win.
    """
    parser = argparse.ArgumentParser(
        prog="probe.py",
        description=(
            "Read-only SwitchBee Central Unit protocol probe. "
            "Connects, performs LOGIN + GET_CONFIGURATION, observes "
            "CONFIGURATION_CHANGE pushes for the requested duration, "
            "then disconnects. Never sends OPERATE."
        ),
        epilog=(
            "Credentials can be supplied via env vars SB_USER / SB_PASS "
            "instead of CLI flags. Explicit flags take precedence."
        ),
    )
    parser.add_argument(
        "--host",
        required=True,
        help="SwitchBee Central Unit host or IP (no scheme).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"CU WebSocket port (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--username",
        "--user",
        dest="username",
        default=None,
        help="CU username. Defaults to $SB_USER if set.",
    )
    parser.add_argument(
        "--password",
        "--pass",
        dest="password",
        default=None,
        help="CU password. Defaults to $SB_PASS if set.",
    )
    parser.add_argument(
        "--duration",
        "--observe-seconds",
        dest="duration",
        type=int,
        default=DEFAULT_DURATION_SECONDS,
        help=(
            f"Seconds to observe CONFIGURATION_CHANGE pushes after the "
            f"initial config fetch (default: {DEFAULT_DURATION_SECONDS}). "
            f"`0` exits right after the config fetch."
        ),
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        default=False,
        help=(
            "REQUIRED for live runs. Asserts the read-only invariant "
            "and lets the probe target a real CU."
        ),
    )
    parser.add_argument(
        "--no-banner",
        action="store_true",
        default=False,
        help="Skip the 5s coexistence banner (for tests and dry-runs).",
    )
    return parser


def _resolve_credentials(args: argparse.Namespace) -> tuple[str, str] | None:
    """Resolve username + password from CLI flags or env vars.

    Returns None if either is missing after the env-var fallback.
    """
    username = args.username or os.environ.get("SB_USER")
    password = args.password or os.environ.get("SB_PASS")
    if not username or not password:
        return None
    return username, password


def _summarize_configuration(data: dict) -> dict:
    """Extract human-friendly summary fields from GET_CONFIGURATION data."""
    raw_mac = data.get("mac")
    normalized_mac = normalize_cu_mac(raw_mac)  # raises CUMACMissingError on failure
    name = data.get("name", "")
    version = data.get("version", "")
    zones = data.get("zones", []) if isinstance(data.get("zones"), list) else []

    type_counter: Counter[str] = Counter()
    total_items = 0
    for zone in zones:
        items = zone.get("items", []) if isinstance(zone, dict) else []
        total_items += len(items)
        for item in items:
            item_type = str(item.get("type", "UNKNOWN"))
            type_counter[item_type] += 1

    return {
        "raw_mac": raw_mac,
        "normalized_mac": normalized_mac,
        "name": name,
        "version": version,
        "total_zones": len(zones),
        "total_items": total_items,
        "by_type": dict(sorted(type_counter.items())),
    }


def _print_summary(summary: dict) -> None:
    """Print the configuration summary to stdout in a stable, greppable form."""
    print("=" * 64)
    print("SwitchBee CU configuration")
    print("=" * 64)
    print(f"  raw mac        : {summary['raw_mac']}")
    print(f"  normalized mac : {summary['normalized_mac']}")
    print(f"  name           : {summary['name']}")
    print(f"  version        : {summary['version']}")
    print(f"  total zones    : {summary['total_zones']}")
    print(f"  total items    : {summary['total_items']}")
    print("  by type:")
    for type_name, count in summary["by_type"].items():
        print(f"    {type_name}={count}")
    print("=" * 64)


def _format_push(event: PushEvent) -> str:
    """Render a single push notification as one stable greppable line."""
    return (
        f"[push] item_id={event.id} "
        f"name={event.name!r} "
        f"new_value={event.value!r}"
    )


def _maybe_print_banner(no_banner: bool) -> None:
    """Print the coexistence banner and sleep the grace window.

    When `no_banner` is True, do nothing — tests and quick dry-runs
    skip the wait. Operators MUST run with the banner for live work.
    """
    if no_banner:
        return
    print(_BANNER, flush=True)
    try:
        time.sleep(BANNER_GRACE_SECONDS)
    except KeyboardInterrupt:
        print("aborted by operator before connect", file=sys.stderr)
        raise SystemExit(130) from None


async def _run_probe(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    duration: int,
) -> int:
    """Execute the probe lifecycle. Returns the desired exit code."""
    async with aiohttp.ClientSession() as session:
        client = SwitchBeeWSClient(
            host,
            username,
            password,
            port=port,
            session=session,
        )

        push_events: list[PushEvent] = []

        def _on_push(event: PushEvent) -> None:
            push_events.append(event)
            print(_format_push(event), flush=True)

        unsubscribe = client.add_listener(_on_push)

        observed_seconds = 0.0
        try:
            print(f"connecting to ws://{host}:{port} ...", flush=True)
            await client.start()
            print("LOGIN ok", flush=True)

            print("fetching GET_CONFIGURATION ...", flush=True)
            data = await client.get_configuration()
            summary = _summarize_configuration(data)
            _print_summary(summary)

            if duration > 0:
                print(
                    f"observing CONFIGURATION_CHANGE for {duration}s ...",
                    flush=True,
                )
                start = time.monotonic()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(duration)
                observed_seconds = time.monotonic() - start
            else:
                print("--duration=0; skipping observation window", flush=True)
        finally:
            unsubscribe()
            await client.stop()

        print(
            f"Observed {len(push_events)} push events in "
            f"{observed_seconds:.1f} seconds. Probe complete.",
            flush=True,
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the desired process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.read_only:
        print(
            "ERROR: refusing to run without --read-only. This script is "
            "read-only by design; pass --read-only to confirm.",
            file=sys.stderr,
        )
        return 2

    credentials = _resolve_credentials(args)
    if credentials is None:
        print(
            "ERROR: missing credentials. Pass --username/--password or set "
            "SB_USER/SB_PASS in the environment.",
            file=sys.stderr,
        )
        return 2

    username, password = credentials

    if args.duration < 0:
        print("ERROR: --duration / --observe-seconds must be >= 0", file=sys.stderr)
        return 2

    _maybe_print_banner(args.no_banner)

    try:
        return asyncio.run(
            _run_probe(
                host=args.host,
                port=args.port,
                username=username,
                password=password,
                duration=args.duration,
            )
        )
    except CUMACMissingError as err:
        print(f"ERROR: CU did not return a usable mac: {err}", file=sys.stderr)
        return 3
    except SwitchBeeProtocolError as err:
        print(f"ERROR: SwitchBee protocol error: {err}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("aborted by operator", file=sys.stderr)
        return 130
    except OSError as err:
        # Connect refused, DNS, etc.
        print(f"ERROR: network failure: {err}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
