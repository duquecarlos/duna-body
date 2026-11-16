# mcp-http-v1 — Body's real MCP endpoint

Built by BODY-MCP-HTTP-1. Carlos, relayed verbatim by the Personal desk 2026-11-16: *"I don't want you to connect with
the body version local. I want you to fully build a real connection to all my production apps ... you'll be the brain
of the smart home ... So you should live with the apps."*

## What this is

One endpoint, `POST https://body.duna.solutions/mcp`, that ANY MCP client adds by URL plus a key -- no Body code
running on the client's own machine, no local process, no stdio pipe. It sits beside the existing hosted door
(`/api/asistente/mcp/herramientas`, `/api/asistente/mcp/llamar`), which keeps serving `conectar_pc`-linked PCs through
Body's own device key unchanged. `/mcp` is retired-the-old-door's eventual replacement, not there yet
(open follow-up).

## Adding it to a Claude session

```
claude mcp add --transport http body https://body.duna.solutions/mcp --header "Authorization: Bearer <key>"
```

`<key>` is typed by the person, by hand, into their own client's configuration -- never stored in a repository, never
logged, never put in a report.

## Auth: the Cuenta Duna assistant key

Every call carries `Authorization: Bearer <key>`. Body checks it against Cuenta Duna's own introspection endpoint,
`POST {issuer}/oauth/introspect`, form body `token=<key>`, Basic auth as Body's own client credentials
(`CLIENT_ID = "body"`) -- never the person's key, which travels only in the form body. This is the SAME key Cuenta
issues once per person, covering every Duna app that person ticked when they granted it (contract:
`duna-cuenta-api/docs/contrato-llave-asistente.md`).

`producto == "body"` on the answer is what proves Body was one of the apps granted. `persona_id` is Cuenta's own
`str(persona.id)` (the same value its OIDC `sub` carries); Body resolves it against its own `person.cuenta_sub` --
the person must have signed in to Body at least once through Cuenta Duna for that link to exist.

| Cuenta's answer | Body's response |
|---|---|
| No `Authorization` header, not `Bearer`, `active: false`, or `producto` is not `"body"` | `401`, `WWW-Authenticate: Bearer`, a generic message -- never saying which of these it was |
| `active: true`, `producto: "body"`, but no Body person has `cuenta_sub` matching `persona_id` | `401`, `WWW-Authenticate: Bearer`, a sentence asking the person to sign in to Body once first |
| Cuenta unreachable, a 5xx, or Cuenta refusing/throttling Body's OWN client credentials (401/429 on the introspection call itself) | `503`, `Retry-After` passed through when Cuenta sent one, a sentence saying to try again in a moment -- never 401: the key itself was never judged |
| Local mode (no hosted Cuenta configured) | `404`, same as every other hosted-only route |

A positive answer is cached at most **60 seconds**, in process memory, keyed by the key's own sha256 (the key itself
is never stored). A negative answer or any fault is **never** cached, so a revoked key stops working within that same
window and a freshly granted one works on its very first call.

## `permiso`: lectura or escritura

Cuenta's answer also carries `permiso`, one of:

* **`lectura`** -- every read tool is listed and callable. A write tool (one that proposes a change, plus
  `aplicar_propuesta`/`descartar_propuesta`) is hidden from `tools/list`, and calling one anyway answers an MCP tool
  result with `isError: true` and a person-language sentence -- never a transport-level error.
* **`escritura`** -- every read AND write tool is listed and callable, and a write applies directly: `escritura`
  stands in for Body's own `aplicar_mcp` setting here (the setting Carlos toggles in Ajustes for the device-key door).
  That setting is never read by this endpoint; granting `escritura` through Cuenta is the whole decision.

`nivel_datos` and Body's own read-only data-level guard apply exactly as they already do at the device-key door --
granting `escritura` never widens what a tool itself is allowed to read or touch.

Every applied write is attributed to the assistant (the key's own `llave_id`, as Cuenta returned it) wherever the
device-key door already attributes one to its generic `mcp` actor -- never to the key itself, which is never logged.

## Transport: Streamable HTTP, stateless

One POST, `Content-Type: application/json`, carrying one JSON-RPC 2.0 request object OR a batch (a JSON array of
them). One `application/json` response -- no SSE stream, no session id, no state kept between calls. `GET /mcp`
answers `405` with `Allow: POST`.

| Method | Behaviour |
|---|---|
| `initialize` | Echoes the client's `protocolVersion` when it is one Body's MCP server already accepts, else the newest version it supports. `serverInfo.name` is `"body"`. |
| `notifications/initialized` | `202`, no body. |
| `ping` | An empty result. |
| `tools/list` | The SAME tool list the local stdio server (`python -m body.mcp`) and the device-key door already share, filtered by `permiso` as above. |
| `tools/call` | The SAME dispatch (caps, `nivel_datos`, proposal creation, apply/discard) the device-key door already runs -- reused, not re-implemented. |
| anything else | `{"error": {"code": -32601, "message": "Method not found"}}` |

Malformed JSON answers `{"error": {"code": -32700, ...}}`; a body that parses but is not a valid JSON-RPC request (or
request batch) answers `{"error": {"code": -32600, ...}}`.

Because the transport is stateless, every `tools/call` that creates a proposal starts its own conversation: there is
no session id a client can send back to group several proposals together the way the Personal desk's own PC client
does. The proposal still appears on Body's Hoy screen exactly the same way.

## What stays unchanged

The existing hosted door (`/api/asistente/mcp/herramientas`, `/api/asistente/mcp/llamar`), Body's own `aplicar_mcp`
setting and its device-key scope, and the local stdio server (`python -m body.mcp`) are all unchanged by this slice.
The shared dispatch both doors already used (`body/mcp.py`'s `_lista_herramientas` / `_llamar_herramienta`) was lifted,
not rewritten, into named functions (`body/rutas/mcp_remoto.py`'s `listar_herramientas` / `llamar_herramienta`) that
`/mcp` now calls too.
