> Main owns this contract; the source is `C:\Duna\dev-protocol\contracts\hogar-v1.md`. This is a read-only copy taken at FORK-1 (22f2b79e): re-copy it from the source, never edit it here.

# hogar-v1 — one household identity across Duna

Decided by Main on 2026-11-06 under ORCH-CARLOS-DELEGATES-TIER-1-1. Ledger: ORCH-HOGAR-EN-CUENTA-1.

## Why

Carlos typed this in the Body app on 2026-11-06, and Body relayed it: his girlfriend gets her own user in Body and in
Finanzas; the accounts link "because we're like a starting family"; they share plans and projects (a joint Kilimanjaro or
El Cocuy plan, linked to the trip in Lugares and to its budget in Finanzas). Three products need THE SAME answer to
"who is in my household", and each one would otherwise invent its own.

## The rule

**Cuenta Duna holds the household as a relationship between personas: who belongs, and that each one consented.
Nothing more.** It holds no money, no shares, no plans and no permissions. Each product keeps its own space and maps the
household onto it:

| Product  | Its space                         | How it follows the household                                      |
|----------|-----------------------------------|-------------------------------------------------------------------|
| Finanzas | a SHARED Entidad tipo "hogar"     | Entidad.id IS the hogar id, given at creation; members synced through the admin plane (agregar_miembro / revocar), never read live per request. **The link is the id, never the tipo:** a person's own Personal space is also tipo "hogar" (household-style books) but belongs to them alone and carries no Cuenta hogar id |
| Lugares  | a lista                           | reads the hogar from the sign-in claims; maps it to a shared lista |
| Body     | its household plans and goals     | reads the hogar from the sign-in claims or the service API         |

Why Cuenta and not a product: consent between two people must be given ONCE and respected by every app. If it lived in
Finanzas, Body and Lugares would depend on a ledger space (and on Finanzas' 100%-shares activation rule) just to know who
someone lives with. Cuenta's rule "it owns WHO, never WHAT" still holds: a household says WHO is with whom; what they
may do together stays in each product.

## Shape (Cuenta)

* `hogar`: `id` (uuid; the id every product uses), `nombre` (optional, set by its members), `creado_en`.
* `hogar_miembro`: `hogar_id`, `persona_id`, `estado` = `invitado` | `activo`, `invitado_por`, `invitado_en`,
  `acepto_en`, `salio_en`. Unique (hogar_id, persona_id). **No cap of two**: "a starting family" grows.
* A persona is in **at most one ACTIVE hogar** at a time. This keeps "my household" unambiguous for every product, and
  it can be widened later without breaking any reader.
* **Joining needs consent from both sides:** an active member invites someone (by email or username), and that person
  accepts while signed in. A one-time link with its own purpose, `hogar`, under the existing single-use and atomic rules.
* **Leaving is always allowed,** by the person themself, at any moment, and takes effect at once. The last member to
  leave closes the hogar. Nobody can remove another member in v1.
* Every change is written to the existing event log (`evento_acceso` or its sibling).

## What products receive

* **Claim** `duna_hogar`: `{"id": "<uuid>", "miembros": ["<sub>", ...]}`, listing only ACTIVE members, or `null`.
  It goes into the ID token, userinfo, and `GET /servicio/cuentas/{sub}`, the same claim set `construir_claims_identidad`
  already builds. A member's `sub` is all a product learns about another person here. Names and emails stay with each
  product's own grant rules.
* A product that needs to react when someone leaves (Finanzas revoking a Membresia) reads the claim at sign-in, or asks
  the service API. A push/webhook is a later version, not v1. Known v1 gap, accepted: a member who left and never
  signs in to a product again keeps a stale membership there until that product polls the service API. This is the
  main reason for the push.

## Privacy

Membership never reveals anything else. A product shows another member's data only under its own rules; Body keeps
health figures private unless a person shares a specific one.

## Order

1. CUENTA-API-HOGAR-1: tables, invite/accept/leave routes, the `hogar` link purpose, the claim.
2. CUENTA-UI-HOGAR-1: the «Hogar» section of the account page (invite, accept, leave), plus the landing for the `hogar`
   link.
3. Products adopt: Finanzas syncs Membresias; Lugares follows LUGARES-HOGAR-CLAIM-1; Body reads the claim once it has a
   Cuenta client (CUENTA-API-CLIENTE-BODY-1, when Body asks for sign-in).

Two questions stay with Carlos and do NOT block 1–2: which Finanzas spaces exist (his Personal, hers, a shared
Household), and the shared space's split.
