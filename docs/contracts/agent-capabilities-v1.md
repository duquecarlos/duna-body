# Duna agent capabilities -- v1

**`ORCH-AGENT-CAPABILITIES-1`, decided by the main orchestrator 2026-10-14 under `ORCH-CARLOS-DELEGATES-TIER-1-1`.**
Carlos, typed in Main 2026-10-14: *"please ask Lotus for what we've been discussing about agent capabilities per product, so
you can orchestrate from here those changes, or at least structure to later fully migrate to those changes in the other
products that apply"*, then *"implement the plan and I'll review that tomorrow morning"*. His rulings this lands (from the
Lotes window, 2026-10-14, verbatim in `C:\Duna\lotes\dev-protocol\contracts\agent-capabilities-lotes-report-2026-10-14.md`):
ruling C, *the assistant drives the app*; *"extend this idea into every other product ... we're a brand more than a single
app"*. And the standing write ruling (`agent-write-contract-v3.md` section 0): *the agent confirms in the chat before it
writes*.

**This contract does not restate the write rules.** `agent-write-contract-v3.md` (rules A-D, amendments 1-7) still governs
WHAT an agent may write and how history is signed. This one governs the SHAPE every product's assistant shares: what a
capability is, how a write is approved, how the screen is driven, what is metered, and who gets what. Inputs measured
2026-10-14 on main: Lotes' report (api 187cad6..3d6b6f3, ui d3ff0db..1eef6c6), and a read-only census of duna-orders,
duna-owner-ui, finanzas, duna-admin-ui and duna-tienda (summarised in section 8).

---

## 1. A capability is a tool, and it declares its KIND

Every tool the model can call is an entry in ONE static registry per product, and each entry carries an explicit kind:

| `tipo` | Writes? | Rule |
|---|---|---|
| `lectura` | never | runs at once; several per round allowed |
| `propuesta` | only after the person approves, through the product's own write path | at most one per round; never applied by the model |
| `accion_pantalla` | never | navegar / resaltar / prellenar / recorrido (section 4); counts as a read |

**DECIDED: the kind is a FIELD on the entry, not a matter of which file the tool lives in.** Today Lotes has `es_escritura`,
Finanzas and Tienda separate registries, Orders a docstring. A field is the one shape a metering row, a gate and a guard can all
read. Products may keep separate files; the entry still says its kind.

**Each product keeps a STATIC guard that a `lectura` tool cannot reach a writer** (Lotes and Tienda have one; Finanzas a
behavioural test; Orders none). Per agent-write-contract-v3, a guarantee nobody asserts is one route from not being one.

## 2. A proposal is a server-side RECORD, approved IN THE CHAT

DECIDED, reconciling four shapes measured today (Lotes and the console: an HMAC-signed card; Tienda: a stored row with an
in-chat card; Finanzas: a stored row applied on a separate screen; Orders: no writes):

1. **The proposal is stored server-side**, keyed by product scope (tenant/entidad), conversation, and the person it was shown
   to, with: the action, the target, the **value before** (for drift), the proposed value, `expira_en`, and a state
   (`propuesta` -> `aplicada` | `descartada` | `expirada`, plus `deshecha` where the product supports undo). This is a business
   record, not a draft of the target table and not transcript -- the reading agent-write-contract-v3 amendment 7 already made, and
   the precondition of its status block.
2. **It is shown as a card IN THE CONVERSATION**, before/after in plain words, with approve and discard. Carlos's ruling is
   confirmation *in the chat*: a product whose only approval path is another screen (Finanzas today) adds the in-chat approval;
   its list screen may stay as the place to review and undo.
3. **Approval is single-use DURABLY** (the state transition in the record, under a row lock or conditional update), checks
   that the caller is the person it was shown to, that it has not expired, and that the target still holds the value before
   -- if not, it refuses with `propuesta_desactualizada` and the agent re-reads. **Single use held in process memory is not
   single use** (the console today) -- a restart or a second instance replays it.
4. **Applying goes through the product's ordinary write path**, the same route a form uses, with the actor recorded as the
   person and the provenance as the agent (agent-write-contract-v3 rule D, section 6).
5. **A signature is allowed, not required.** A product may sign the card (Lotes, the console) so a client cannot forge one; the
   durable record is what makes approval safe either way.
6. Expiry default **15 minutes**; a product may choose shorter, never longer than 24 h.

## 3. The model learns its own writes from the record

Agent-write-contract-v3 amendment 7, unchanged and now required everywhere: each turn the server renders a status block of
this conversation's proposals and their current state from the record; it outranks the model's own earlier turns.

## 4. Screen actions -- the Lotes contract, adopted Duna-wide

`C:\Duna\lotes\dev-protocol\contracts\asistente-accion-ui-v1.md` (v1 + amendment section 5) is **adopted as the Duna
contract**, copied by meaning, not by stack:

* the model calls one tool `accion_en_pantalla` {accion, lugar, valores, pasos}; the server VALIDATES and answers at once; the
  UI receives an ephemeral `accion_ui` event (or, for a non-streaming product, an `acciones_ui` array in the response);
* the UI OWNS a registry of PLACES (stable `nombre`, `pantalla`, `proposito`, allowed `acciones`, `parametros`, `roles`; a
  `data-lugar` attribute on the element; an integrity test that a declared place still has its element) and sends it with
  every request with `lugar_actual`; the server filters places by the caller's role and offers the tool only when places came;
* **`prellenar` never writes** -- the person saves through the same path as typing;
* ceilings as Lotes amendment section 5; over a ceiling the request is refused, never truncated.

## 5. Gating: role yes, plan reserved

* **Role:** each entry may declare the roles that get it (default: every role allowed to use the assistant); the server
  filters tool definitions per request, as Lotes filters places. Product role names stay the product's (Orders owner/support,
  Finanzas session/machine, Tienda OWNER/MANAGER, console admin).
* **Plan:** a reserved optional field `plan_minimo` on the entry, filtered at the same place. **Nobody fills it yet** -- there
  are no plans to gate on -- but the field exists so a plan tier is data, not a rewrite.

## 6. Metering vocabulary

One shared shape, so the console can read every product's assistant cost the same way:

* **One usage row per assistant REQUEST** (a person's question), with: product scope, person, when, provider, model, tokens
  `entrada`, `salida`, `cache_lectura`, `cache_escritura`, `rondas`, `motivo_fin`, error code, whether it was a question,
  surface. A product that meters per model call (Orders) or per round (Tienda) keeps that detail if it wants, but also
  produces the per-request row (a view is acceptable).
* **Cost is DERIVED from tokens and a price table**, never the only stored truth. A stored cost (Tienda's `costoMicroUsd`) may
  stay as a cache, recomputable.
* **Limits, two kinds and only two:** a monthly QUESTION quota that **never refuses** (Carlos: *"we never cut them off"*), and
  SPEND ceilings that **do refuse** (daily required; monthly optional), in the product's currency minor unit. Token ceilings
  (Orders) are an implementation of a spend ceiling, not a third kind. **A ceiling fails CLOSED** when its own read fails
  (Tienda fails open today).
* The console's ceiling is per admin and per day; a platform-wide total may be added on top, not instead.

## 7. Providers, history, transport

* Anthropic, OpenAI and Gemini behind one provider interface per product (all five have it).
* History: agent-write-contract-v3 amendments 5-6 (server-authored turns signed) apply where the CLIENT carries the history
  back. Where the server stores the history (Orders, Tienda), signing is not required; the status block (section 3) is.
* Transport is the product's (SSE or JSON). Screen actions and proposals travel in both.

## 8. Where each product stands, and its migration

Measured 2026-10-14. **Each product moves on its own queue; nothing here is deployed by this document.**

| | Kind field | Proposals | Screen actions | Metering | Limits |
|---|---|---|---|---|---|
| **Lotes** | `es_escritura` -> `tipo` | signed card; confirm the durable record + single-use (ask) | **built** server side; UI dispatch after Carlos's screen review | per request, no cache split | quota + daily ceiling: conforms |
| **Pedidos** (orders + owner-ui) | none -> add | **none** (read-only by ruling); keep until a write tool is ruled, then section 2 | none -> places registry in owner-ui, then actions | per call + reservations -> add per-request row, cached tokens | quota, daily, monthly tokens: conforms (tokens = spend) |
| **Finanzas** | separate registries -> add field | stored row, applied on a separate screen -> **add in-chat approve** | none | per request; split `cacheada` | conforms (daily + monthly) |
| **Tienda** (Edgar) | separate registries -> add field | stored row + in-chat card + drift: **conforms** | none -> first after Lotes (Carlos: Tienda next) | per round, stored cost -> add per-request view | daily ceiling **fails open** -> fail closed |
| **Console** | ~22 reads, ~30 `*_propose`, add field | signed card; **single use in memory -> durable record** | proposal row marks only -> places registry | per request via Orders | platform-wide daily -> per admin + total |

Main dispatches Pedidos and the console. Lotes and Finanzas receive this contract and run their own rows. Tienda's row goes to
Edgar in the Tienda-first document Lotes wrote (`https://claude.ai/code/artifact/7b83fc6c-8e50-49b1-82f0-a6f70985bd86`).

## 9. Still open, and whose

* **What the Pedidos owner's assistant may WRITE** (it is read-only by ruling today). Carlos's, when he wants it.
* **Plans** (section 5): exist when Duna's plans are a product thing.
* Amendments to sections 1-7 come from any product as proposals to Main, which decides and appends here.

---

## AMENDMENT 1 -- 2026-10-14 ~21:30, proposed by Duna Lotes, ACCEPTED by Main (section 2)

Lotes measured its `propuesta_agente` (duna-lotes-api main): server-side, keyed by `conversacion_id`, single use durable by
UNIQUE (tenant_id, ref) and (tenant_id, batch_id) with a second approval answering `ya_aplicada` -- it conforms to section 2's
purpose. Two fields differ by design, and section 2 is amended so the RULE is the guarantee, not the column:

* (a) **The value before is required only for a proposal that CHANGES an existing value** (update-shaped: Tienda's
  precio/stock/activo/estado, the console's settings). An insert has no value before.
* (b) **Where a product holds a confidentiality ruling against storing figures** (Lotes: LOTES-DATOS-CONFIDENCIALIDAD-1,
  Carlos), the value before may travel in the server-SIGNED card instead of the stored record -- **provided the server re-reads
  the current value at approval and refuses on drift**. The drift check is the requirement; where the value is kept is not.
* (c) **Expiry may be enforced through the signed envelope** rather than a column, provided it is checked server-side at
  approval.

What does NOT move: the record exists server-side, keyed by conversation; single use is durable; approval is in the chat;
applying goes through the ordinary write path.

## AMENDMENT 2 -- 2026-10-14 ~21:40, raised by Duna Finanzas, DECIDED by Main (section 6, token arithmetic)

Finanzas measured that its `cacheada` is CONTAINED IN `entrada` and priced at its own rate, and that the vendors disagree
(one reports input EXCLUDING cache reads and writes; others report prompt tokens INCLUDING cached ones). Without a rule, two
products' costs would not compare. DECIDED: **the three input counts are DISJOINT.**

* `entrada` = input tokens billed at the model's BASE input rate only -- excluding cache reads and cache writes;
* `cache_lectura` = input tokens served from cache; `cache_escritura` = input tokens written to cache;
* total input = `entrada + cache_lectura + cache_escritura`; `salida` separate;
* **cost = sum of each count x its own rate**, no subtraction anywhere downstream;
* **each provider adapter normalises** to this at the boundary (a vendor that reports prompt tokens including cached ones has
  the cached part subtracted there, once). A vendor that does not separate writes reports `cache_escritura = 0`.

A product migrating a contained column (Finanzas' `cacheada` inside `entrada`) converts existing rows in the same migration so
history and new rows mean the same thing, and says in its ledger how it did it.

## AMENDMENT 2, CLARIFICATION -- 2026-10-15, from duna-orders ORDERS-AGENTE-USO-POR-PREGUNTA-1 (ccec96c7)

Amendment 2 binds the **per-request row the contract exposes**, not a product's storage column. Measured in duna-orders: its
stored `input_tokens` stays the WHOLE input, because its existing monthly token ceiling reads that column unchanged; the
per-question views derive `entrada = input_tokens - cache_lectura - cache_escritura`. That conforms: what another product or
the console reads is disjoint. A product may store either form; it must say which in its ledger, and the per-request row it
exposes is disjoint. The console (duna-admin-ui 5e7484c) follows Orders' storage convention when it reports usage.

## AMENDMENT 3 -- 2026-10-15, DECIDED by Main (section 4, pre-filling a list-shaped form)

Measured by ADMIN-AGENTE-PRELLENAR-1 (duna-admin-ui a187721): two console forms cannot be described by `parametros` as a flat
list of field names -- Pedidos' weekly hours (a list of time slots per day) and «Dar acceso» (its fields load only after a
product is chosen). DECIDED:

* **A parameter may be a LIST of items** with its own item fields: `{nombre, tipo: "lista", campos: [names], max_items}`
  (max_items <= 50). `valores` for it is an array of objects keyed only by `campos`. Validation and ceilings as before; an
  item over `max_items` refuses the call, never truncates.
* **A form whose fields depend on an earlier choice** declares each dependent field as a separate PLACE reached after that
  choice (e.g. «Dar acceso -- Pedidos»), not as conditional parameters. The assistant first navigates or highlights the
  choice; the person makes it; the dependent place then exists and can be pre-filled. Nothing is pre-chosen for the person.
* Still true: `prellenar` never writes, never submits, marks every filled field «Sugerido por el asistente» with a dismiss.

## AMENDMENT 4 -- 2026-10-16, raised by Duna Finanzas, ADOPTED by Main (section 4, what the model is told about places)

Measured by Finanzas, then by Main in the other three: **the model never sees a place's `proposito` or `parametros`.** Lotes
(`construir_parametros_accion_en_pantalla`, agente/herramientas.py ~1218), Pedidos' owner assistant
(`build_accion_en_pantalla_tool`, duna-orders services/owner_agent_tools.py:1914) and the console
(`accion_en_pantalla`, duna-admin-ui src/lib/admin-agent-tools.ts ~246) all offer only an enum of place NAMES; the `valores`
description refers to "the keys that place declares" without showing them. So a `prellenar` guesses its keys (and is refused
for an unknown one), and "where is X" is answered from names alone. Every product carries `proposito` and never reads it.
DECIDED:

* **The tool description carries a deterministic CATALOGUE of the places offered**, one entry per place, in registry order:
  `nombre: proposito. Acciones: ... Valores: <param> (<tipo>[, <unit>])...`; a list parameter (amendment 3) shows its
  `campos` and `max_items`. A money parameter states its unit in words (Finanzas: `monto_cop` in centavos). Places with no
  parameters omit «Valores».
* **Byte-identical for the same registry and role**, so the cached prefix holds: no timestamps, no `lugar_actual`, no
  per-request ordering. `lugar_actual` stays in the message, never in the tool.
* **The UI sends its WHOLE static registry on every request, never a per-screen subset**, for the same reason: the tool
  schema sits in the cached prefix. The server's role filter is the only thing that narrows it.
* The catalogue counts toward the existing ceilings; a registry whose catalogue would exceed them is refused at the
  integrity test in the UI, never truncated at request time.

Carriers: Finanzas FIN-AGENTE-ACCION-PANTALLA-1-B (running); Pedidos ORDERS-AGENTE-LUGARES-CATALOGO-1; console
ADMIN-AGENTE-LUGARES-CATALOGO-1; Lotes, by its own orchestrator.

## AMENDMENT 5 -- 2026-10-18, DECIDED by Main (section 6, a model with no price)

The security census (ORCH-CARLOS-SECURITY-CENSUS-1) found the same defect in three products: a model, provider or token kind
with no price is costed at 0, so the spend ceiling never trips (Finanzas SEC-FINANZAS-API-1, Lotes SEC-LOTES-API-1, Pedidos
SEC-ORDERS-1, all HIGH), and two ceilings fail OPEN when their own read fails (Lotes, Pedidos SEC-ORDERS-2). Section 6 already
said "a ceiling fails CLOSED"; this makes the unpriced case explicit. DECIDED:

* **Nothing is ever costed at 0 because its price is missing.** When the request's model (or any token kind it uses: cache
  read/write, images) has no price, the spend ceiling for that request falls back to a **COUNT breaker**: requests per scope
  per day, default **200**, overridable per scope as data. Over it, the product's existing daily-limit refusal (429 and its
  sentence). Lotes built exactly this (LOTES-API-TOPE-DIARIO-CONTEO-1); it is now the Duna rule.
* **A ceiling whose own read fails refuses** (the product's limit refusal, logged). An earlier fail-open choice made for
  availability (Pedidos tenant_ai_spend_cap) is superseded: when the database cannot be read, the assistant cannot do useful
  work anyway, and an unbounded bill is the worse failure.
* The console's own staff agent follows the same rule per admin (SEC-ADMIN-UI-FIX-1 refuses an unpriced model outright; the
  count breaker is an acceptable alternative there).

## STATUS NOTE -- 2026-10-24, Main (section 8's table is history, not current state)

Section 8's table and section 9's open questions were true on 2026-10-14 and are now STALE for Pedidos and the console;
the table stays as written (this file is append-only for its history). Measured 2026-10-24 by
ADMIN-AGENT-CAPABILITIES-V1-CENSUS-1 and OWNER-AGENT-CAPABILITIES-V1-CENSUS-1: Pedidos' owner assistant PROPOSES five
write kinds, approved in the chat (Carlos, ORCH-CARLOS-PEDIDOS-ASISTENTE-PROPONE-1, 2026-10-15; OWNER-AGENTE-PROPUESTAS-
CARD-1, OWNER-AGENT-DESHACER-1), carries a places registry with navegar/resaltar/prellenar, and meters per request; the
console HAS sections 1-7 and amendments 1-5 except one unlogged failed ceiling read. Measure a product's code, not this
table.
