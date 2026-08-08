# Troubleshooting

Symptom → fix. Full setup flow: [SETUP.md](SETUP.md).

---

## Install and config files

| Symptom | Fix |
|---------|-----|
| `Failed to find config.json` on `mythic-cli install` | Run `sync_install_trees.sh`; install from `$REPO/install/epoch` or `$REPO/install/chronos`, not `$REPO/epoch` |
| Two `config.json` confusion | Install wrapper is `$REPO/install/epoch/config.json`; runtime file is `~/Mythic/InstalledServices/epoch/c2_code/config.json` — payload build creates the runtime file (SETUP Step 7) |
| `FileNotFoundError: c2_code/config.json` in Mythic UI | Complete SETUP Step 7 before Step 8 — build payload to run config check |
| Start Profile `JSONDecodeError` / empty config | `c2_code/config.json` is missing or 0 bytes — rebuild Chronos payload; file must be valid JSON |
| Config check: failed to fetch credentials file | Re-upload service account JSON in Epoch instance; save, then rebuild payload |

---

## Epoch server

| Symptom | Fix |
|---------|-----|
| `docker logs epoch` shows only RabbitMQ | Read `~/Mythic/InstalledServices/epoch/c2_code/server.log` instead |
| Saved calendar ID but log shows `Using calendar: primary` | **Stop → Start** Epoch profile; rebuild agent if calendar ID changed |
| Check-in event never deleted on calendar | Epoch is on wrong calendar or not running — check `Using calendar:` in `server.log` |
| Endless “Waiting for credentials file…” | Run SETUP Step 7 first; verify `c2_code/credentials.json` exists next to `config.json` |

---

## Agent and callback

| Symptom | Fix |
|---------|-----|
| No callback after long wait | Same calendar ID on Epoch and built agent; Epoch started and Accepting Connections; wait full 30s–4min budget |
| Agent exits on check-in failure | Default behavior — fix Epoch/calendar config; for lab debug only, rebuild with `force_resume_on_checkin_fail` |
| Tasks queued but no responses | One task at a time until first response returns; Calendar visibility lag |
| Stale callback after restart | Clear `~/.chronos_*` or build a new payload for a cold start |

---

## Mythic UI

| Symptom | Fix |
|---------|-----|
| epoch / chronos not listed | Re-run SETUP Step 5; check `docker ps` for epoch and chronos containers |
| Cannot reach Mythic UI | Open port **7443** on firewall; use `https://` not `http://` |

---

## GCP / Google

| Symptom | Fix |
|---------|-----|
| `iam.disableServiceAccountKeyCreation` | SETUP Step 2.3 — relax org policy or use a project where key creation is allowed |
| Calendar API errors / 403 | Enable Calendar API; share calendar with service account (**Make changes to events**) |
| Wrong calendar | Paste full `@group.calendar.google.com` ID from calendar settings on both Epoch and rebuilt agent |
