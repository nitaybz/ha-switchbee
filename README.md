# ha-switchbee

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant custom integration for the **SwitchBee Central Unit**, talking to the CU directly over its local WebSocket protocol on port `7891`. No cloud, no homebridge bridge, no HomeKit pairing dance. Push-based state updates from the CU surface as native HA state changes within ~1 second.

## Features (target)

- Local-only operation. The integration speaks WebSocket to the CU on the same LAN; no internet traffic.
- Push state updates via the CU's `CONFIGURATION_CHANGE` notifications. Periodic polling is a fallback, used only when the WebSocket is disconnected.
- Supports the SwitchBee device classes the upstream homebridge plugin covers: switches, dimmers, shutters, and Somfy curtains. Scene activation is exposed as HA `scene.*` entities. Out-of-scope for v1: IR, thermostat, VRF AC, sensor, two-way relay.
- HA-native entity types: `switch`, `light`, `cover`, `scene`.
- Token cache with automatic re-login on `INVALID_TOKEN`. LOGIN-never-replies is handled by closing the dead WebSocket and reconnecting, not by retrying on the same connection.

## Minimum Home Assistant version

**Home Assistant Core `2024.1.0` or newer.**

This pin is set in both `hacs.json` and `custom_components/ha_switchbee/manifest.json`. The lower bound is locked to `2024.1.0` because the migration tool (Phase 5) relies on `entity_registry.async_get_or_create` adopting orphan rows by `(platform, unique_id)` while preserving `entity_id`, `original_name`, `area_id`, `aliases`, `labels`, `icon`, and other user-customized fields. This adoption semantic exists at HA core `2024.1.0` and remains in place at recent HA releases (verified via the HA core source).

## Installation (HACS Custom Repository)

The integration is installed as a HACS Custom Repository. There is no Ginnie PC installer hook; this repo is a stand-alone HA custom integration.

1. In HACS, open **Integrations** → menu (top-right) → **Custom repositories**.
2. Add `https://github.com/nitaybz/ha-switchbee` as a repository with category **Integration**.
3. Install **SwitchBee Local** from the HACS Integrations list.
4. Restart Home Assistant.
5. Go to **Settings** → **Devices & services** → **Add integration** → search for **SwitchBee Local**. (Config flow lands in Phase 3.)

## Configuration

Configuration is via the HA UI (config flow). Required fields:

- **Host**: the IP address of the SwitchBee Central Unit on the LAN.
- **Username**: the CU username.
- **Password**: the CU password.

The integration discovers all devices and zones via `GET_CONFIGURATION`, mirrors them as HA entities, and listens for push state updates.

### Options

Open **Settings → Devices & services → SwitchBee Local → Configure** to tune:

- **Connection timeout** (seconds, default `5`): WebSocket LOGIN and per-command timeout.
- **Poll interval** (seconds, default `60`, `0` to disable): periodic reconciliation. The CU pushes every state change in real time over the WebSocket; the poll is a defensive safety net for any push that gets dropped (transient WS drop, missed event, etc). Every N seconds the integration fetches `GET_MULTIPLE_STATES` for every known item and dispatches an update only for items whose state differs from the cache. Set to `0` to rely on pushes exclusively.

## Coexistence with `homebridge-switchbee`

During QA and rollout, `ha-switchbee` is designed to run side-by-side with the existing `homebridge-switchbee → homekit_controller` integration on the same Home Assistant instance, without `unique_id` collision.

The two integrations use structurally disjoint unique-ID grammars:

- `homekit_controller` unique-IDs look like `0E:0F:B5:1B:3D:37_75_8` (colon-separated uppercase bridge MAC, then `_aid_iid` or `_aid_sid_iid`).
- `ha-switchbee` unique-IDs look like `a82108e768af_3` (12-character lowercase contiguous hex MAC of the SwitchBee CU, then `_<item.id>`).

Both grammars are checked statically and against a fixture before release; no homebridge entity can ever collide with an `ha-switchbee` entity.

A separate migration tool (Phase 5) takes a stopped Home Assistant, finds the SwitchBee-shaped `homekit_controller` entity-registry rows, rewrites them in-place to `(platform=ha_switchbee, unique_id=<new-format>)` with `config_entry_id=None`, and lets the running integration adopt them on next startup. The migration preserves `entity_id`, `friendly_name`, `area_id`, `aliases`, `labels`, `icon`, and `original_name` for every real device. The HomeKit-only `button.*_identify` entries are removed during migration since they have no SwitchBee analog.

## Branding

The integration's brand mark lives in `custom_components/ha_switchbee/brand/`:

- `icon.png` (256x256, transparent canvas, SwitchBee wordmark centered)
- `icon@2x.png` (512x512 retina)
- `logo.png` (790x256, full SwitchBee wordmark + tagline)
- `logo@2x.png` (1580x512 retina)

## License

`ha-switchbee` is licensed under the **MIT** license (see [`LICENSE`](LICENSE)).

It is a clean-room Python rewrite of the SwitchBee WebSocket protocol, informed by but not copied from the JavaScript source in [`homebridge-switchbee`](https://github.com/nitaybz/homebridge-switchbee). Both projects are authored by the same person (`nitaybz`), so the same author has license-grant authority over both code bases. The upstream `homebridge-switchbee` `package.json` declares `license: GNU`, which is not a valid SPDX identifier; this rewrite is published under a standard SPDX-compliant MIT grant.
