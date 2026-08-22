# Changelog

## 0.2.0

- Add opt-in YAML listing and redacted, bounded file reads.
- Add exact-patch, append, and new-file staging with SHA-256 stale-write protection.
- Validate YAML syntax locally and run Home Assistant's own full configuration check while
  atomically restoring the original file afterward.
- Require a checked change and approval code for apply; mark apply and rollback tools as
  destructive MCP operations so the client can request confirmation.
- Add atomic writes, protected pre-change backups, conflict-aware rollback, and startup recovery
  for interrupted checks, applies, or rollbacks.
- Continue to block service calls, entity-state changes, reloads, restarts, secrets, system paths,
  arbitrary shell commands, and non-YAML writes.

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
