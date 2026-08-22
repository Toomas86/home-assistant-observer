# Home Assistant Observer for ChatGPT

This app gives ChatGPT persistent, privacy-redacted Home Assistant diagnostics and optional, transactional YAML configuration management. It uses OpenAI's outbound-only Secure MCP Tunnel, so you do not open a router port and do not expose Home Assistant itself.

## Before installing

You need:

1. Access to [OpenAI Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels) with Tunnels Read + Manage to create a tunnel.
2. A runtime API key with Tunnels Read + Use. Do not use an admin key.
3. ChatGPT developer-mode access and permission to create a custom app/plugin in the intended workspace.

The tunnel must be associated with both the Platform organization and the ChatGPT workspace where it will be used. New role assignments can take up to 30 minutes to propagate.

## 1. Enable persistent Home Assistant error events

Add or merge this block in `configuration.yaml`:

```yaml
system_log:
  max_entries: 200
  fire_event: true
```

If `system_log:` already exists, add only the missing options under it. Check the configuration and restart Home Assistant. Without `fire_event: true`, ChatGPT can still inspect HA's current error buffer, but the app cannot retain every new warning/error as it happens.

## 2. Create the OpenAI tunnel

In OpenAI Platform:

1. Open **Settings → Tunnels**.
2. Create a tunnel associated with the correct Platform organization and ChatGPT workspace.
3. Copy its `tunnel_...` ID.
4. Create a runtime API key with only **Tunnels Read + Use** and copy it once.

Treat the runtime key like a password. The app stores it in Home Assistant's protected options and copies it to a mode-`0600` file for `tunnel-client`; it is never passed on the command line.

## 3. Install the Home Assistant app

In Home Assistant OS:

1. Open **Settings → Apps** (called **Add-ons** on older HA versions).
2. Open the app store menu and choose **Repositories**.
3. Add `https://github.com/Toomas86/home-assistant-observer`.
4. Install **Home Assistant Observer for ChatGPT**.
5. In its Configuration tab, set:

```yaml
tunnel_id: tunnel_your_id_here
openai_runtime_api_key: sk-your-runtime-key
retention_days: 30
allow_sensitive_entities: false
config_management_enabled: false
log_level: info
```

6. Start the app and enable **Start on boot** and **Watchdog** if Home Assistant offers them.
7. Check the app log. Both the MCP observer and the outbound OpenAI tunnel must remain running.

The first installation builds the image locally and may take several minutes on a Raspberry Pi. Tailscale may remain installed, but this app does not use or expose it.

## 4. Connect it in ChatGPT

1. Enable developer mode in ChatGPT under **Settings → Security and login** if it is not already enabled.
2. Open [ChatGPT Plugins](https://chatgpt.com/plugins), choose the plus button, and create a developer-mode app.
3. Select **Tunnel** as the connection type.
4. Select the tunnel created above, or paste its `tunnel_...` ID.
5. Review the discovered tools. Diagnostic, listing, reading, and preview tools are read-only. Staging and checking are non-destructive writes; apply and rollback are destructive, confirmation-requiring tools.
6. Save/connect the app.

Useful first tests:

- “Kontrolli Home Assistanti vaatleja ühendust ja näita viimased viis viga.”
- “Leia pesumasinaga seotud olemid ja näita nende praeguseid olekuid.”
- “Näita automaatika `pesumasin_odavstart` kolme viimast trace'i ja selgita ebaõnnestumisi.”
- “Näita aktiivseid Home Assistant Repairs teateid.”

## Optional YAML configuration management

Leave `config_management_enabled: false` unless you want ChatGPT to edit Home Assistant YAML. To opt in:

1. Open the app configuration in Home Assistant.
2. Set `config_management_enabled: true` and restart only this app.
3. Refresh/reconnect the custom ChatGPT app if the new tools are not visible.

The required workflow for an existing file is:

1. `ha_read_config_yaml` returns a bounded, redacted excerpt and the raw file's SHA-256 digest.
2. Stage one exact replacement with `ha_stage_yaml_patch`, or an append with `ha_stage_yaml_append`. A new file uses `ha_stage_new_yaml_file`.
3. Inspect the redacted diff with `ha_preview_staged_yaml`.
4. Run `ha_check_staged_yaml`. The app takes a protected backup, atomically places the candidate, calls Home Assistant's own `POST /api/config/core/check_config`, and restores the original before returning. A crash-recovery journal restores interrupted checks on the next app start.
5. If and only if Home Assistant reports `valid`, explicitly confirm `ha_apply_staged_yaml`. The current file must still have the digest seen during staging.
6. Reload the affected YAML integration or restart Home Assistant separately and manually if needed. Version 0.2.0 deliberately provides no reload or restart tool.
7. If required, explicitly confirm `ha_rollback_yaml_change` with the rollback code returned by apply. Rollback refuses to overwrite a file changed after apply.

The manager accepts only UTF-8 `.yaml`/`.yml` files up to 1 MiB. It blocks hidden paths, traversal, symlinks, `secrets.yaml`, `.storage`, custom components, backups, ESPHome, SSL, web assets, and arbitrary nested folders. Staged literal credentials are rejected; keep them in `secrets.yaml` and reference them with `!secret`.

## Available MCP tools

| Tool | Access |
|---|---|
| `ha_get_overview` | HA version, entity counts, unavailable/unknown entities, collector status |
| `ha_get_recent_errors` | Structured current and retained warnings/errors |
| `ha_search_errors` | Text search over structured retained/current errors |
| `ha_search_raw_error_log` | Matching lines from the current-session raw HA error log |
| `ha_find_entities` | Concise entity search by ID/name, domain, or state |
| `ha_get_entities` | Full sanitized state/attributes for exact entity IDs |
| `ha_get_history` | Up to seven days of history for exact entities |
| `ha_get_logbook` | Up to seven days of logbook events for one entity |
| `ha_get_automation_traces` | Recent automation trace summaries and full trace details |
| `ha_get_repairs` | Active Repairs issues only; no repair flow is started |
| `ha_list_config_yaml` | Allowed YAML paths, sizes, modification times, and SHA-256 digests |
| `ha_read_config_yaml` | Bounded, redacted YAML excerpt plus the raw file digest |
| `ha_stage_yaml_patch` | One exact staged replacement; does not change live YAML |
| `ha_stage_yaml_append` | One staged append; does not change live YAML |
| `ha_stage_new_yaml_file` | One staged new file in an approved folder |
| `ha_preview_staged_yaml` | Redacted unified diff and stage status |
| `ha_check_staged_yaml` | HA full config check with automatic original-file restoration |
| `ha_apply_staged_yaml` | Confirmed atomic apply; no reload or restart |
| `ha_rollback_yaml_change` | Confirmed conflict-aware restore from the protected backup |

## Security notes

- The MCP listener and tunnel health listener bind only to loopback inside the app container.
- The app requests `homeassistant_api: true`. Its client implements REST `GET`, one fixed admin-only `POST /api/config/core/check_config`, and an explicit allowlist of read-only WebSocket commands. It has no generic POST method or service-call command.
- Home Assistant's configuration folder is mounted at `/homeassistant` because app mount permissions are static. All YAML tools still enforce `config_management_enabled`, path and file-type allowlists, symlink rejection, size bounds, stale-digest checks, and credential filtering in code.
- Check/apply backups live in protected app data with mode `0600`; they are never returned by an MCP tool. A backup can contain the original file, so remove the app data when permanently uninstalling.
- It has no Supervisor manager role, Docker socket, host network, device access, or arbitrary command endpoint.
- ChatGPT/workspace app invocation and compliance logs are separate from the tunnel transport. Do not deliberately request secrets through diagnostic tools.
- Turning on `allow_sensitive_entities` permits metadata from cameras, people, device trackers, images, and geolocation entities; content such as camera images is still not fetched by this app.

## Troubleshooting

### Tunnel is not visible in ChatGPT

Verify that it is associated with the target ChatGPT workspace, not only the Platform organization, and that your user has Tunnels Read + Use plus ChatGPT developer-mode access.

### The app stops immediately

Check that both required configuration fields are set. The log will report whether the MCP server or `tunnel-client` stopped; secrets are not printed.

### No retained errors appear

Confirm `system_log.fire_event: true`, restart HA after editing `configuration.yaml`, and check `ha_get_overview` → `observer.events_received` after a new warning occurs.

### Automation traces fail with an admin error

Trace retrieval is an admin-only HA WebSocket command. Ensure the app is running under Home Assistant OS with `homeassistant_api: true` and has not been manually stripped of its Supervisor token.

## Revocation and removal

To fully disconnect:

1. Disconnect/delete the custom app in ChatGPT.
2. Stop and uninstall this Home Assistant app and remove its app data (the local SQLite database, staged metadata, and protected YAML backups are removed with it).
3. Revoke the OpenAI runtime API key.
4. Delete the OpenAI tunnel if nothing else uses it.
