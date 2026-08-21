# Changelog

## 0.1.3

- Start through Home Assistant's `with-contenv` Bashio wrapper so the scoped
  `SUPERVISOR_TOKEN` reaches the observer process.

## 0.1.2

- Read app options with `jq` from `/data/options.json` because current Bashio
  versions fetch `bashio::config` through the Supervisor API regardless of the
  `CONFIG_PATH` environment variable.

## 0.1.1

- Read app options directly from `/data/options.json` so startup works without
  granting access to the Supervisor API.

## 0.1.0

- Initial experimental release.
- Read-only entity, history, logbook, repairs, error-log, and automation-trace tools.
- Sanitized SQLite retention for `system_log_event` warnings and errors.
- Outbound-only OpenAI Secure MCP Tunnel packaged in the same Home Assistant app.
