# Home Assistant Observer for ChatGPT

A minimal Home Assistant OS app (add-on) that gives ChatGPT privacy-redacted diagnostics and optional, transactional YAML configuration management through an [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

The Home Assistant MCP server listens only on `127.0.0.1` inside its container. The bundled tunnel client makes an outbound HTTPS connection to OpenAI; no router port, public URL, Cloudflare tunnel, or Tailscale exposure is required.

## What ChatGPT can inspect

- current and retained Home Assistant warnings/errors;
- raw current-session error-log lines matching a search term;
- exact entity states and attributes;
- entity history and per-entity logbook events;
- automation trace summaries and full trace details;
- active Repairs issues;
- HA version, entity counts, and unavailable/unknown entities.

It intentionally has no tool for service calls, state changes, reloads, restarts, repair flows, or automation execution.

## Optional YAML management

YAML access is disabled by default. After the Home Assistant owner explicitly enables `config_management_enabled`, ChatGPT can:

- list and read redacted YAML from approved configuration paths;
- stage an exact replacement, append, or new YAML file without changing the live file;
- preview a redacted unified diff;
- run Home Assistant's own full configuration check while the candidate is temporarily present, then restore the original;
- apply a checked change only through a separately confirmed, destructive MCP tool;
- roll back from a protected pre-change backup if the file has not changed since apply.

Each existing-file change requires the current SHA-256 digest, so a stale request cannot overwrite newer work. Writes are atomic. `secrets.yaml`, `.storage`, custom components, backups, ESPHome, SSL, web assets, hidden paths, path traversal, symlinks, credential-like values, and non-YAML files are blocked.

## Privacy defaults

- Credentials, bearer/JWT tokens, secret query parameters, IP/MAC addresses, latitude, and longitude are redacted.
- System-log events are redacted before being written to `/data/observer.db`.
- `camera`, `person`, `device_tracker`, `image`, and `geo_location` entities are blocked unless explicitly enabled.
- YAML configuration management is off by default, and literal credentials in staged text are rejected in favor of `!secret` references.
- There is no published container port. Home Assistant supplies a scoped `SUPERVISOR_TOKEN` at runtime; it is never placed in app options or the repository.

See [the installation guide](ha_observer/DOCS.md) for the complete setup.

## Repository layout

- `ha_observer/`: Home Assistant app package.
- `ha_observer/rootfs/app/observer/`: MCP server, HA client, and transactional YAML manager.
- `tests/`: privacy, persistence, path-policy, transaction, recovery, and API-contract tests.

## Local tests

```bash
python3 -m pip install PyYAML==6.0.2
python3 -m unittest discover -s tests -v
python3 -m compileall -q ha_observer/rootfs/app/observer tests
shellcheck ha_observer/run.sh
```

## Status

Experimental `0.2.0`. Start with both optional access settings off and a narrowly scoped OpenAI runtime API key.
