# Home Assistant Observer for ChatGPT

A minimal Home Assistant OS app (add-on) that gives ChatGPT persistent, read-only access to diagnostics through an [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

The Home Assistant MCP server listens only on `127.0.0.1` inside its container. The bundled tunnel client makes an outbound HTTPS connection to OpenAI; no router port, public URL, Cloudflare tunnel, or Tailscale exposure is required.

## What ChatGPT can inspect

- current and retained Home Assistant warnings/errors;
- raw current-session error-log lines matching a search term;
- exact entity states and attributes;
- entity history and per-entity logbook events;
- automation trace summaries and full trace details;
- active Repairs issues;
- HA version, entity counts, and unavailable/unknown entities.

It intentionally has no tool for service calls, state changes, configuration edits, repair flows, or automation execution.

## Privacy defaults

- Credentials, bearer/JWT tokens, secret query parameters, IP/MAC addresses, latitude, and longitude are redacted.
- System-log events are redacted before being written to `/data/observer.db`.
- `camera`, `person`, `device_tracker`, `image`, and `geo_location` entities are blocked unless explicitly enabled.
- There is no published container port. Home Assistant supplies a scoped `SUPERVISOR_TOKEN` at runtime; it is never placed in app options or the repository.

See [the installation guide](ha_observer/DOCS.md) for the complete setup.

## Repository layout

- `ha_observer/`: Home Assistant app package.
- `ha_observer/rootfs/app/observer/`: read-only MCP server and HA client.
- `tests/`: dependency-free privacy, persistence, input-policy, and static read-only tests.

## Local tests

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q ha_observer/rootfs/app/observer tests
shellcheck ha_observer/run.sh
```

## Status

Experimental `0.1.3`. Start with `allow_sensitive_entities: false` and a narrowly scoped OpenAI runtime API key.
