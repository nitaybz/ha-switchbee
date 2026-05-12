# ha-switchbee Cutover Runbook

This runbook flips a single customer device from the legacy
`homebridge-switchbee -> homekit_controller` chain to the native
`ha-switchbee` Home Assistant custom integration.

It is doc only. Nothing runs automatically. An operator with SSH + sudo on
the target Ginnie PC follows the steps below, gated on explicit user
go-ahead per device.

The runbook is intentionally generic. The only customer-specific knowledge
needed at execution time is the env values listed in section 1 (host, CU
host, CU credentials, bridge MAC, bridge config_entry_id).

## Conventions

All shell snippets assume the operator has set the following environment
variables (do not paste secrets into the runbook itself):

```bash
export GINNIE_HOST="<tailscale ip or hostname of the customer ginnie-pc>"
export SUDO_PASS="<sudo password for the ginnie user on that box>"
export SB_USER="<switchbee CU username>"
export SB_PASS="<switchbee CU password>"
export CU_HOST="<lan ip of the switchbee central unit>"
export CU_MAC="<12-hex mac of the CU, uppercase, colon-separated>"
export BRIDGE_MAC="<homekit_controller bridge mac as it appears in HA, e.g. 0E:0F:B5:1B:3D:37>"
export BRIDGE_CONFIG_ENTRY_ID="<ULID of the homekit_controller config_entries row for the SwitchBee bridge>"
```

SSH to the box uses the `SSH_ASKPASS` pattern (per global CLAUDE.md). Make
sure `/tmp/ssh_pass.sh` is set up locally before the session:

```bash
echo '#!/bin/bash
echo "$SUDO_PASS"' > /tmp/ssh_pass.sh && chmod +x /tmp/ssh_pass.sh
SSH_OPTS="SSH_ASKPASS=/tmp/ssh_pass.sh SSH_ASKPASS_REQUIRE=force DISPLAY=:0"
# Example usage shown in each step below:
#   eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST "<remote command>"
```

The `sudo` invocations inside the remote box rely on the same password
prompt indirection; do not hardcode the password into shell pipelines.

## 1. Pre-flight

Run these checks once per target device before considering cutover. If any
item fails, fix it first; do not proceed.

- HA storage path verified at `/home/ginnie/ginnie-home/ha/.storage/`,
  containing `core.entity_registry`, `core.device_registry`,
  `core.area_registry`, `core.config_entries`.
- `homebridge-switchbee` plugin currently installed and currently
  configured against the same `$CU_HOST` with valid `$SB_USER` /
  `$SB_PASS`. Verify via `/var/lib/homebridge/config.json`:
  ```bash
  eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
      "sudo -S cat /var/lib/homebridge/config.json | jq '.platforms[] | select(.platform==\"SwitchBee\")'"
  ```
  Expect a single SwitchBee platform block with the customer's CU host.
- Operator can SSH to the box as `ginnie` with sudo. Confirm:
  ```bash
  eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST "sudo -S whoami"
  ```
  Expect `root`.
- HA itself has a fresh snapshot (HA's own backup mechanism, not the
  migration tarball). Operator checks via the HA UI: Settings -> System ->
  Backups. The most recent backup must be < 24h old.
- The `ha-switchbee` custom integration is already deployed to the HA
  config directory:
  ```bash
  eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
      "ls /home/ginnie/ginnie-home/ha/config/custom_components/ha_switchbee/manifest.json"
  ```
  Either HACS-installed or manually copied. The HA UI does NOT yet have a
  ha-switchbee config entry; that gets added at the end of section 4.
- All env vars listed in the Conventions section are exported in the
  operator's shell.

## 2. Live coexistence probe

The probe is the verified-safe Phase 6 step. It opens a parallel WS
session to the CU while homebridge-switchbee continues to hold its own WS
session. Some CU firmware revisions accept concurrent clients; some do
not. Always re-verify per device before assuming it is safe.

Run the probe from the operator workstation (or from the box itself, with
the repo cloned). 60-second window is enough:

```bash
python tools/probe.py \
    --host "$CU_HOST" \
    --user "$SB_USER" \
    --pass "$SB_PASS" \
    --duration 60 \
    --read-only
```

In a second terminal, tail the homebridge log on the box and grep for
switchbee output:

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
    "sudo -S tail -f /var/lib/homebridge/homebridge.log | grep -i switchbee"
```

What you need to observe during the 60 seconds:

- Probe completes with exit 0.
- Homebridge bridge PID does NOT change. Confirm with
  `sudo systemctl status homebridge` in a third tty before and after; the
  `Main PID:` line must match.
- The switchbee-watchdog cron line does NOT fire. Watchdog log lives at
  `/var/log/switchbee-watchdog.log` if the operator has it enabled; tail
  it and confirm no restart entry was appended during the probe window.
- The homebridge log does NOT show a SwitchBee disconnect / reconnect
  storm during the window.

If the probe disconnects homebridge (CU rejects concurrent WS), abort
forward path and switch to Option B: stop homebridge for the duration of
cutover only, and document the change of plan before continuing.

> Real-world reference: a Phase 6 run against the STE Smart Home CU
> firmware tier did NOT disrupt homebridge; that CU accepts concurrent WS
> clients. Other CUs may differ. Always re-verify per device.

## 3. Dry-run migration

Always run a dry-run first. It produces the backup tarball and the
operator-readable report. The dry-run does NOT mutate HA storage.

HA must be running for the dry-run (the tool refuses to apply against
running HA, but dry-run is safe). Run from the box itself:

```bash
OUTPUT_DIR="/home/ginnie/ha-switchbee-migration-$(date +%Y%m%d-%H%M%S)"
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST bash <<EOF
set -euo pipefail
cd /home/ginnie/ha-switchbee
python tools/migrate.py \\
    --dry-run \\
    --ha-storage /home/ginnie/ginnie-home/ha/.storage \\
    --homebridge-persist-dir /var/lib/homebridge/switchbee-persist \\
    --homebridge-config /var/lib/homebridge/config.json \\
    --cu-host "$CU_HOST" \\
    --cu-user "$SB_USER" \\
    --cu-pass "$SB_PASS" \\
    --cu-mac "$CU_MAC" \\
    --bridge-mac "$BRIDGE_MAC" \\
    --output-dir "$OUTPUT_DIR"
EOF
```

Verify the dry-run produced all three artifacts:

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
    "ls -la $OUTPUT_DIR/{backup.tar.gz,report.json,report.md}"
```

Operator opens `report.md` and reviews:

- Total migrated count matches the operator's expectation (compare against
  the device's current Settings -> Devices entity count for the SwitchBee
  bridge).
- High-confidence rows (`confidence: high`) look right. Spot check 2-3 by
  cross-referencing the friendly name against the HA UI.
- Low-confidence rows (`confidence: low` or marked `keep_homekit`):
  operator decides per row. For v1, low-confidence rows STAY on the
  homekit_controller bridge after cutover. They are NOT migrated. Manual
  remapping is out of scope.
- Delete rows are all `button.*_identify` entities and nothing else.
  Anything else in the delete list is a red flag; do not proceed.

If anything in the report is wrong, stop here. The dry-run is the
operator's last cheap exit.

## 4. Forward cutover

Operator gives explicit go-ahead before this section runs. Skipping the
go-ahead gate defeats the purpose of the runbook.

Order matters. The plan locks in this sequence because the migration
tool's tarball must snapshot `/var/lib/homebridge/config.json` while the
SwitchBee platform block is STILL PRESENT; rollback restores from the same
tarball, so the strip must happen AFTER the apply.

### 4.1 Stop the switchbee-watchdog cron

The watchdog detects homebridge process death and restarts it. While we
are mid-cutover, that is exactly the wrong thing.

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
    "sudo -S crontab -u root -l | grep -v switchbee-watchdog | sudo -S crontab -u root -"
```

Verify the watchdog line is gone:

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
    "sudo -S crontab -u root -l | grep -i watchdog"
```

Expect no output.

### 4.2 Stop Home Assistant

Migration tool refuses to mutate registries while HA is running, because
HA caches the registry in memory and would overwrite our edits on its next
flush.

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
    "cd /home/ginnie/ginnie-home && docker compose stop ginnie-home"
```

Confirm:

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
    "cd /home/ginnie/ginnie-home && docker compose ps ginnie-home --format json | jq -r '.[0].State'"
```

Expect `exited`.

### 4.3 Run the migration with --apply

Same flag set as the dry-run, plus `--apply` and
`--bridge-config-entry-id`. The tarball includes the unmodified
`/var/lib/homebridge/config.json` for rollback symmetry.

```bash
OUTPUT_DIR="/home/ginnie/ha-switchbee-migration-$(date +%Y%m%d-%H%M%S)"
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST bash <<EOF
set -euo pipefail
cd /home/ginnie/ha-switchbee
python tools/migrate.py \\
    --apply \\
    --ha-storage /home/ginnie/ginnie-home/ha/.storage \\
    --homebridge-persist-dir /var/lib/homebridge/switchbee-persist \\
    --homebridge-config /var/lib/homebridge/config.json \\
    --cu-host "$CU_HOST" \\
    --cu-user "$SB_USER" \\
    --cu-pass "$SB_PASS" \\
    --cu-mac "$CU_MAC" \\
    --bridge-mac "$BRIDGE_MAC" \\
    --bridge-config-entry-id "$BRIDGE_CONFIG_ENTRY_ID" \\
    --output-dir "$OUTPUT_DIR"
echo "APPLY_OUTPUT_DIR=$OUTPUT_DIR"
EOF
```

Capture `$OUTPUT_DIR` from the output; it is the rollback artifact path.
Verify the same three files exist (`backup.tar.gz`, `report.json`,
`report.md`) and inspect the new `report.md` to confirm the actual mutate
counts match the dry-run preview.

At this point the HA registries are mutated, but HA itself is still
stopped and no integration has loaded the orphan rows yet.

### 4.4 Disable the homebridge SwitchBee platform

Edit `/var/lib/homebridge/config.json` to remove (or comment out) the
SwitchBee platform block. Use a wrapper edit; do NOT delete unrelated
platform blocks.

The simplest in-place strip with jq:

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST bash <<'EOF'
set -euo pipefail
sudo -S cp /var/lib/homebridge/config.json /var/lib/homebridge/config.json.pre-cutover
sudo -S jq '.platforms |= map(select(.platform != "SwitchBee"))' \
    /var/lib/homebridge/config.json.pre-cutover \
    | sudo -S tee /var/lib/homebridge/config.json.new > /dev/null
sudo -S mv /var/lib/homebridge/config.json.new /var/lib/homebridge/config.json
sudo -S chown homebridge:homebridge /var/lib/homebridge/config.json
EOF
```

Verify the SwitchBee platform block is gone:

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
    "sudo -S jq '.platforms[] | select(.platform==\"SwitchBee\")' /var/lib/homebridge/config.json"
```

Expect no output (no matching block).

> The `config.json.pre-cutover` copy is a belt-and-suspenders backup. The
> migration tarball already contains the same content; this copy is only
> for quick same-box recovery without untarring.

### 4.5 Restart homebridge

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
    "sudo -S systemctl restart homebridge"
```

Wait ~10s, then confirm homebridge is healthy and no longer trying to
reach the CU:

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
    "sudo -S systemctl is-active homebridge && sudo -S tail -50 /var/lib/homebridge/homebridge.log | grep -i switchbee || echo 'no switchbee log entries (expected)'"
```

### 4.6 Start Home Assistant

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
    "cd /home/ginnie/ginnie-home && docker compose up -d ginnie-home"
```

Wait for HA to finish booting. Tail the HA log until you see the standard
"Home Assistant initialized" line:

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
    "cd /home/ginnie/ginnie-home && docker compose logs -f ginnie-home | grep -m1 'Home Assistant initialized'"
```

### 4.7 Add the ha-switchbee integration via the HA UI

This is the final step that closes the loop. HA's
`async_get_or_create((platform=ha_switchbee, unique_id=<cu_mac>_<item_id>))`
will adopt the orphan entity_registry rows written by step 4.3, preserving
every entity_id, original_name, area_id, icon, alias, and label, and
filling in the new config_entry_id.

Operator steps in the HA UI:

1. Settings -> Devices & Services -> Add Integration.
2. Search "SwitchBee Local" (or "ha-switchbee", depending on the
   manifest's title), select it.
3. Enter `$CU_HOST`, `$SB_USER`, `$SB_PASS` in the config flow form.
4. Submit. The new ha-switchbee config entry appears in the integrations
   list with the migrated entity count under it.

## 5. Post-cutover verification

Stay on the device while doing these checks. Do not declare cutover done
until all five pass.

- **Switch toggles**: pick one known SwitchBee switch entity (operator
  picks based on customer device naming). Toggle it from the HA UI; the
  physical relay must click and the entity state must update.
- **Physical push back to HA**: have the customer (or operator) press the
  physical wall switch for that same load. The HA entity state must
  update within ~1s (the P4 SLO). If it takes > 1s, capture the HA log
  for the coordinator and review before proceeding to additional zones.
- **CU device card**: HA UI -> Settings -> Devices, look for a single
  device card for the SwitchBee Central Unit, with the migrated child
  entities listed under it.
- **Automation regression**: pick one automation that targets a migrated
  entity. Trigger it (manually from the automation page if needed) and
  confirm it still fires. Automations should NOT need editing; the
  entity_id is preserved.
- **No homebridge errors**: tail the homebridge log; no switchbee-related
  errors or restart loops should appear.

### Re-enable the watchdog (optional)

The `switchbee-watchdog` cron only checks homebridge process liveness. It
does NOT check that homebridge is actually reaching the CU. After
cutover, homebridge no longer talks to the CU at all, so the watchdog is
mostly redundant.

If the operator wants to keep homebridge alive as a safety net (e.g. so
non-SwitchBee HomeKit accessories on the same homebridge instance keep
working), re-enable it:

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST \
    "sudo -S crontab -u root -e"
```

Add back the original `switchbee-watchdog.sh` line (operator should have
captured it before section 4.1).

If the operator has no other HomeKit accessories on this homebridge
instance, leave the watchdog off; ha-switchbee has its own internal
watchdog (LOGIN timeout -> reconnect with backoff) baked into the
protocol module.

## 6. Rollback

Use rollback when a post-cutover check fails or when the customer reports
a regression. Three rollback windows exist, with different costs.

### Window A: during `migrate.py --apply`

The tool's safety gates refuse to mutate if anything is off (HA still
running, output dir under tmpfs, missing bridge_config_entry_id, etc).
If the apply died mid-write, the backup tarball is the truth.

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST bash <<EOF
set -euo pipefail
cd /home/ginnie/ginnie-home && docker compose stop ginnie-home
cd /home/ginnie/ginnie-home/ha/.storage
sudo -S cp -a core.entity_registry core.entity_registry.bad-\$(date +%s)
sudo -S cp -a core.device_registry core.device_registry.bad-\$(date +%s)
sudo -S tar -xzf $OUTPUT_DIR/backup.tar.gz -C /home/ginnie/ginnie-home/ha/.storage/ \\
    ./core.entity_registry ./core.device_registry ./core.area_registry ./core.config_entries
sudo -S tar -xzf $OUTPUT_DIR/backup.tar.gz -C /var/lib/ ./homebridge/config.json
sudo -S systemctl restart homebridge
sudo -S crontab -u root -e   # operator manually re-adds the watchdog line
cd /home/ginnie/ginnie-home && docker compose up -d ginnie-home
EOF
```

### Window B: after `--apply`, before HA restart

Same as Window A. HA has not yet loaded the new state, so restoring the
tarball is a clean revert.

### Window C: after HA has restarted and automations have triggered

Hardest case. Use BOTH the migration tarball (registry state) AND the HA
snapshot from section 1 pre-flight (in case any automation persisted
state during the post-cutover window).

```bash
eval "$SSH_OPTS" ssh -o StrictHostKeyChecking=no ginnie@$GINNIE_HOST bash <<EOF
set -euo pipefail
cd /home/ginnie/ginnie-home && docker compose stop ginnie-home

# Preserve the broken state for post-mortem
cd /home/ginnie/ginnie-home/ha/.storage
sudo -S cp -a core.entity_registry core.entity_registry.bad-\$(date +%s)

# Restore registry state from the migration tarball
sudo -S tar -xzf $OUTPUT_DIR/backup.tar.gz -C /home/ginnie/ginnie-home/ha/.storage/ \\
    ./core.entity_registry ./core.device_registry ./core.area_registry ./core.config_entries

# Restore homebridge config from the same tarball (SwitchBee platform block returns)
sudo -S tar -xzf $OUTPUT_DIR/backup.tar.gz -C /var/lib/ ./homebridge/config.json

# Restart homebridge so it reconnects to the CU under the legacy plugin
sudo -S systemctl restart homebridge

# Re-enable the watchdog cron line
sudo -S crontab -u root -e

# Bring HA back up
cd /home/ginnie/ginnie-home && docker compose up -d ginnie-home
EOF
```

After HA boots, delete the orphan ha-switchbee config entry from the HA
UI: Settings -> Devices & Services -> ha-switchbee -> "Delete". The
restored entity_registry rows are already in homekit_controller shape, so
this leaves no orphans.

If a deeper rollback is needed (e.g. customer automations broke during
the post-cutover window), restore the operator's HA-native snapshot from
section 1 pre-flight via the HA UI: Settings -> System -> Backups ->
restore the pre-cutover snapshot. The migration tarball is still the
authoritative source for the four registry files plus homebridge config;
the HA snapshot covers everything else (automations, scripts, scenes).

## 7. Known caveats

- **Concurrent WS support varies per CU firmware (A1).** The STE Smart
  Home CU tier accepts concurrent WS clients, which is what makes the
  read-only probe in section 2 non-disruptive there. Other firmware tiers
  may reject concurrent clients. Always run the section 2 probe per
  device before assuming coexistence is safe. If the probe disrupts
  homebridge, switch to Option B (stop homebridge for the duration of
  cutover) and document the deviation.
- **Low-confidence rows stay on homekit_controller.** Any entity the
  mapper flagged as `confidence: low` or `keep_homekit` in the dry-run
  report does NOT get migrated. Those entities continue to work through
  homebridge after cutover. They will appear under the existing
  homekit_controller bridge in HA, unchanged. Manual remapping is out of
  scope for v1.
- **Backup retention.** Keep the migration tarball (`$OUTPUT_DIR/`) for
  at least 30 days post-cutover. It is the only rollback path for
  Window A and Window B, and the canonical artifact for Window C. After
  30 days, if no rollback was needed, the operator may delete it. Do not
  store the tarball under `/tmp`; the migration tool already refuses
  output dirs under tmpfs because Debian clears `/tmp` on reboot.
- **Watchdog post-cutover.** The `switchbee-watchdog` cron checks
  homebridge process liveness only, not functional state. After cutover,
  homebridge no longer reaches the CU regardless of whether it is
  running. Re-enabling the watchdog after cutover is a per-operator
  decision: keep it on if homebridge is still serving other accessories;
  leave it off if homebridge is now idle. ha-switchbee has its own
  internal LOGIN-timeout reconnect logic and does not depend on the
  external watchdog.
