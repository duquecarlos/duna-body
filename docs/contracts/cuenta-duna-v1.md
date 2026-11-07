# Cuenta Duna contract, v1 -- what every Duna product does to sign people in through Cuenta Duna

Owner: dev-protocol (the main orchestrator), which also owns `duna-cuenta-api` and `duna-cuenta-ui`. Each product's
half is built by that product's owner: Pedidos and the console here, Lotes and Finanzas by their orchestrators, Tienda
in the Tienda lane. Written 2026-10-07 from `research/cuenta-duna-design-2026-10-07.md` (the design; its section
numbers are cited as "design N"). Ledger: ORCH-CARLOS-CUENTA-DUNA-1, ORCH-CARLOS-CUENTA-DUNA-D1-1.

**What this is.** Carlos, 2026-10-07: one Duna account for every product. Cuenta Duna owns WHO a person is and how
they prove it. Each product keeps WHAT that person may do in it: memberships, roles, tenants. Identity is central and
authorization is local.

**How it is held.** Four relying-party implementations exist (Python x3, Better Auth), and there is no shared client
library (design 7.1). They are held to one behaviour by `contracts/cuenta-duna-v1/vectores.json`: signed tokens with
the verdict each MUST reach. **A product's relying-party tests run every vector.** `verificar_vectores.py` beside it is
the reference verifier. `generar_vectores.py` regenerates the file with a fresh key and never commits a private key.

MUST, MUST NOT and SHOULD are used in the RFC 2119 sense.

---

## 1. Parties

| | Value |
|---|---|
| Issuer (`iss`) | `https://cuenta.duna.solutions`, compared as an exact string (a trailing slash is a different issuer) |
| Discovery | `GET https://cuenta.duna.solutions/.well-known/openid-configuration` |
| Keys | `GET /jwks.json`; RS256 only, 3072-bit |
| Passkey RP ID | `cuenta.duna.solutions`, the exact host. Every ceremony happens at Cuenta; products never run one |

**Clients.** A static registry written by migration, with secrets from each host's env. There is no dynamic
registration. Every URI is an exact-string match: no wildcards and no query variation.

| `client_id` | Product | `redirect_uri` | Back-channel logout URI |
|---|---|---|---|
| `pedidos` | owner `/panel` + kitchen `/cocina` (`app.duna.solutions`) | `https://app.duna.solutions/login/cuenta/vuelta` | loopback, on duna-orders |
| `consola` | admin console (`admin.duna.solutions`) | `https://admin.duna.solutions/login/cuenta/vuelta` | loopback, on duna-admin-ui's server |
| `lotes` | `lotes.duna.solutions` | `https://lotes.duna.solutions/auth/cuenta/callback` | loopback, on duna-lotes-api |
| `finanzas` | `finanzas.duna.solutions` | `https://finanzas.duna.solutions/iniciar-sesion/cuenta/vuelta` | loopback, on duna-finanzas-api |
| `tienda` | the Tienda panel (`web.duna.solutions`) | `https://web.duna.solutions/api/auth/oauth2/callback/cuenta-duna` (Better Auth's path shape, to be confirmed by TIENDA-CUENTA-RP-1) | HTTPS |

**Registered client URIs, as each product reports them** (the migration that writes `cliente_oidc` takes them from
here; a product changes its row by messaging the owner, never by editing this table):

| `client_id` | `post_logout_redirect_uri` | `initiate_login_uri` | back-channel path (loopback) |
|---|---|---|---|
| `lotes` | `https://lotes.duna.solutions/entrar` | `https://lotes.duna.solutions/auth/cuenta/entrar` | duna-lotes-api `/auth/cuenta/logout-token` |
| `pedidos` | `https://app.duna.solutions/login` | `https://app.duna.solutions/login/cuenta/entrar` (OWNER-UI-CUENTA-INICIAR-1, duna-owner-ui 7089cca; registered by CUENTA-API-CLIENTES-URIS-2, 2026-10-12) | duna-orders `/auth/cuenta/logout-canal` |
| `consola` | `https://admin.duna.solutions/login` | `https://admin.duna.solutions/login/cuenta/entrar` (ADMIN-UI-CUENTA-INICIAR-1, duna-admin-ui e24108f; registered by CUENTA-API-CLIENTES-URIS-2, 2026-10-12) | duna-admin-ui's server `/api/cuenta-duna/logout` (loopback; exempt from the console's session guard for POST since 6f375fb) |
| `finanzas` | `https://finanzas.duna.solutions/iniciar-sesion` | `https://finanzas.duna.solutions/iniciar-sesion/cuenta/entrar` | duna-finanzas-api `/autenticacion/cuenta/logout-token` |
| `tienda` | (set by TIENDA-CUENTA-RP-2) | `https://web.duna.solutions/api/auth/cuenta-duna/iniciar-sesion` (TIENDA-CUENTA-RP-1, 2026-10-11; `?prompt_none=1` starts a silent flow) | none: Tienda is not co-located; its back-channel URI is HTTPS on web.duna.solutions, path in TIENDA-CUENTA-RP-1's ledger entry |

Finanzas' `redirect_uri` is `https://finanzas.duna.solutions/iniciar-sesion/cuenta/vuelta`; all three UI paths derive
from `FINANZAS_UI_PUBLIC_URL` (FIN-CUENTA-DUNA-RP-1, reported 2026-10-09 from `web/app.py`).

Each client row also carries `post_logout_uris`, `initiate_login_uri` (section 8), `acr_minimo` and `max_age`. **For every client except `consola`: `acr_minimo` = `pwd` (any method) and no `max_age`.** `consola`: `passkey` once enrolled, 12 h.
Co-located products reach `/oauth/token` and `/jwks.json` over loopback (`CUENTA_BASE_URL_INTERNA`) and still
validate `iss` against the public issuer.

## 2. Sign-in flow

OpenID Connect Core 1.0, **authorization code flow only**. **PKCE `S256` is REQUIRED for every client**, even though
every client is confidential.

1. **Start (product backend).** The component that mints the product's session today starts the flow (design 2.4:
   whatever sets the cookie today keeps setting it). It generates `state`, `nonce` and a PKCE verifier and stores them
   in a short-lived row keyed by sha256 of a random id: single use, 10 minutes. The browser gets that id in an
   httpOnly, Secure, **SameSite=Lax**, host-only cookie (`flujo`). It must be Lax, not Strict, so it survives the
   top-level return from Cuenta.
2. **Authorize (browser, top-level, never an iframe).** `GET /oauth/authorize` with `response_type=code`,
   `client_id`, `redirect_uri`, `scope=openid`, `state` (REQUIRED), `nonce` (REQUIRED), `code_challenge`,
   `code_challenge_method=S256`, and optionally `prompt=none|login`, `max_age`, `acr_values`, `login_hint`.
3. **Return.** Cuenta redirects to the `redirect_uri` with `code` and `state`, or with `error` (section 10). The
   product relays `{code, state, flujo}` to its backend, which checks `state` against the stored row and consumes it.
4. **Exchange (server to server).** `POST /oauth/token` with `client_secret_basic`, `grant_type=authorization_code`,
   `code`, `redirect_uri` and `code_verifier`. Codes live 60 s, are single use, and are stored hashed. **Reusing a
   code revokes everything issued from it.** The response carries an `id_token`. There is no `refresh_token`
   (section 6), and the `access_token` is only good for `/oauth/userinfo`, which a product does not need.
5. **Validate** the ID token exactly as section 3.2 and the vectors say.
6. **Link and authorize locally** (sections 4 and 5), then mint the product's own session as today.

**Not offered in v1:** implicit and hybrid flows, the password grant, dynamic registration, refresh tokens,
`offline_access`, front-channel logout.

## 3. The ID token

### 3.1 Claims

| Claim | Always present | Content |
|---|---|---|
| `iss` | yes | the issuer, exact |
| `sub` | yes | an opaque UUID, the person's ONLY key (section 4) |
| `aud` | yes | the `client_id` |
| `exp` | yes | 5 minutes after issue |
| `iat`, `auth_time` | yes | standard |
| `nonce` | yes | echoes the request |
| `sid` | yes | the Cuenta session id; a product stores it on its own session row (section 7) |
| `acr` | yes | `pwd` or `passkey` |
| `amr` | yes | standard |
| `name` | yes | display name |
| `duna_productos` | yes | array of the `client_id`s this person may enter, for the switcher (section 8) |
| `duna_personal` | yes | `true` for Duna staff |
| `email`, `email_verified` | **NO** | present only when the account has an email. **A username-only account carries neither claim**: not empty, not null, absent |
| `preferred_username` | NO | present only when the account has a username |

**D1 (Carlos, 2026-10-07): an account holds an email, a username, or both.** So:

* **A product MUST NOT require `email`.** Vector `id-valido-solo-usuario` fails any product that does. A product whose
  user table has `email NOT NULL` makes the column nullable in its relying-party slice. It SHOULD also replace its
  UNIQUE on email with UNIQUE `cuenta_id` (design 5.2), so that an email change at Cuenta never collides with a stale
  local copy.
* `email` and `preferred_username` are **display data** that the product may cache and refresh at each sign-in.
  **Neither is ever a key.**
* `email_verified: false` is normal. A link handed over in person does not verify an email (design 5.2).

### 3.2 Validation (every vector is one of these rules)

A product MUST reject, with the vector's `codigo`:

| Rule | `codigo` |
|---|---|
| `alg` is anything but RS256. The algorithm is pinned, never read from the header | `alg` |
| `kid` not in the JWKS, even after ONE refetch (refetch at most once a minute) | `kid` |
| the signature does not verify | `firma` |
| `iss` is not the exact issuer | `iss` |
| `aud` is not exactly this `client_id` as a single string (Cuenta always issues ONE audience; a list is refused) | `aud` |
| `exp` more than 60 s in the past | `exp` |
| `iat` more than 60 s in the future | `iat` |
| `nonce` absent or not the stored one | `nonce` |
| `sub` absent | `sub` |
| the client's `acr_minimo` is not met, compared as a RANK, `pwd` < `passkey` (a passkey sign-in always meets a `pwd` minimum; an equality check would refuse it -- caught by Lotes, 2026-10-07) | `acr` |

The leeway is **60 seconds** for every time check. A verifier under test takes its clock from the vector's `ahora`.

## 4. Linking `sub` to the product's user (design 2.6, 6.3)

* **`sub` is assigned once, never reissued, never reassigned, and never derived from an email or username.** It does
  not change on re-sign-in, any credential change, an email or username change, deactivation and reactivation, or a
  key rotation.
* **Each product links `sub` to exactly ONE local user row, in a `cuenta_id` column that is UNIQUE and IMMUTABLE once
  written.** So the product's own user id is stable across every re-sign-in. Lotes' offline queue stamps the
  Lotes-local id and depends on this.
* **Existing users are linked ONCE, by the migration, never by matching emails at sign-in.** Cuenta's import emits
  `vinculos-<producto>.csv` (`id_local, sub`). Each product ships a `vincular_cuenta` command that reads it, sets
  `cuenta_id`, is idempotent, and refuses to change a link already set. Carlos runs it. A follow-up migration makes
  `cuenta_id` NOT NULL once the run is measured complete; unlinked rows are listed, never dropped.
* **At sign-in a product looks up its user BY `sub` ONLY.** A `sub` with no linked row and this product in
  `duna_productos` is a person the product has not seen. The product creates its local user row linked to that `sub`
  and treats it as having zero memberships (section 5) until a membership exists. **Never "find the user by email".**
  Matching by email at sign-in would hand an existing user to whoever controls that address at Cuenta, and a
  hand-delivered link does not verify an email.
* A person is never merged after migration. A duplicate is deactivated and its `sub` stays retired.

## 5. Tenants are the product's (design 2.5)

**Cuenta Duna never chooses a tenant.** After the callback, a product resolves the linked user's ACTIVE memberships:

| Active memberships | The product MUST |
|---|---|
| 0 | show its own «Sin acceso» page, with the switcher and «Cerrar sesión», never a 403 dead end. Copy shape: «Tu Cuenta Duna no tiene acceso a ningún negocio en Duna Lotes. Pídele acceso al administrador.» |
| 1 | mint the session for it, as today |
| more than 1 | show its own chooser, and offer «Cambiar de negocio» in session without a new Cuenta round trip |

Lotes' `miembro_de_otro_tenant` and Finanzas' `EmailYaEsMiembro` exist only because there was no chooser. Retiring them
is recommended, and it is those orchestrators' call.

## 6. Sessions

| Session | Rule |
|---|---|
| Cuenta's own (`cuenta_sesion`, host-only on `cuenta.duna.solutions`) | idle 14 days sliding, absolute 90 days |
| Each product's | **its own, as today**, minted by what mints it today. **Contract cap: at most 30 days without a Cuenta round trip** |
| `consola` | `acr=passkey` once the staff member has one, and `max_age` 12 h. Cloudflare Access stays in front |
| A change to a credential, an email or a username (at Cuenta) | `auth_time` within 10 minutes, or re-prompt |

* **There are no refresh tokens.** When a product session ends, the product sends the browser TOP-LEVEL to
  `/oauth/authorize?prompt=none`. If Cuenta's session is alive, a code comes back with no screen. If not, the answer
  is `error=login_required` and the product starts a visible sign-in. **Never in an iframe.**
* **Every cookie stays host-only.** No cookie, Cuenta's or a product's, is ever set on `.duna.solutions`.
* **When Cuenta is down,** existing product sessions keep working until they expire, because no product calls Cuenta
  per request. New sign-ins fail closed with «No pudimos conectar con Cuenta Duna. Intenta de nuevo en unos minutos.»
  **There is no local fallback password.**

## 7. Sign-out (design 2.2)

* **«Cerrar sesión» in any product is GLOBAL.** The product ends its own session, then sends the browser to
  `/oauth/logout?client_id=<its id>&post_logout_redirect_uri=<registered>&logout_hint=<the sid it stored>`. **A product
  does NOT store the ID token and does NOT send `id_token_hint`** (an ID token lives 5 minutes; keeping it for a 14-day
  session to echo it back is a stored bearer for nothing -- Finanzas, 2026-10-07). Cuenta ends its session only when
  `logout_hint` equals the `sid` of the session its own cookie names; any other request (no hint, a different `sid`)
  gets a confirmation screen, «¿Cerrar tu sesión de Cuenta Duna?», so a forged link cannot sign anyone out. Cuenta then
  fans out a back-channel logout to every client holding a session under that `sid`.
* **A product MUST store `sid` on its own session row** (one nullable column), so a logout token can find the
  session.
* **The back-channel endpoint** (OIDC Back-Channel Logout 1.0) accepts `POST`, form-encoded `logout_token=<jwt>`,
  and answers **200** when it ended the sessions (or found none) and **400** on an invalid token. It reaches no other
  service and sets no cookie. The logout token MUST pass section 3.2's `alg`, `kid`, `firma`, `iss`, `aud` and `exp`
  checks, plus:

| Rule | `codigo` |
|---|---|
| header `typ` is not `logout+jwt` (so an ID token can never be replayed here) | `typ` |
| `events` lacks `http://schemas.openid.net/event/backchannel-logout` | `events` |
| it carries `nonce` | `nonce` |
| it has neither `sub` nor `sid` | `sub_o_sid` |
| its `jti` was already seen (keep seen `jti`s until their `exp`, in DURABLE storage: an in-process set loses them on a restart and reopens the replay window after every deploy -- Lotes, 2026-10-07) | `jti` |

* **Scope.** With `sid`: end EVERY local state carrying that `sid` -- sessions, and also a pre-session row a product
  keeps while the person sits on «Sin acceso» or the chooser (Finanzas' sessions are tenant-scoped, so its chooser runs
  on such a row) -- and answer 200. **With `sub` and no `sid`: end EVERY local
  session of the user linked to that `sub`.** Cuenta sends the second form on deactivation, after a recovery, on
  «Cerrar sesión en todos los dispositivos», and on a staff action. Revoking one product's grant sends it to that
  product only.
* **Delivery.** Cuenta writes each logout to an outbox in the same transaction as the event, and retries with back-off
  for 24 h. An entry still undelivered raises a `warn` flag in the console. A product that was down receives it late
  and MUST still honour it.

## 8. The switcher, «Tus productos»

* Each product renders «Tus productos» from the `duna_productos` claim it received at sign-in, plus «Mi Cuenta Duna»
  (`https://cuenta.duna.solutions/cuenta`).
* Each entry links to `https://cuenta.duna.solutions/ir/<client_id>`. Cuenta checks its session and the grant, then
  redirects to that client's `initiate_login_uri` (OIDC Core 4, third-party-initiated login). The product starts a
  `prompt=none` flow and the person lands with **no screen shown** while Cuenta's session is alive.
* **`/login` on every product redirects straight to Cuenta.** There is no intermediate button. A product page appears
  only for errors, «Sin acceso» and the chooser. Cuenta's screen names the requesting product: «Entra para continuar a
  Duna Lotes».

## 9. Cuenta's service API (product to Cuenta)

Authenticated with the product's own client credentials (`client_secret_basic`). **A client can act only on its OWN
product.**

| Route | Body | Answer |
|---|---|---|
| `POST /servicio/cuentas/asegurar` | `{email?, usuario?, nombre}`, at least one of `email`/`usuario` | `{sub, nueva, puede_entrar, enlace_activacion?}`. Finds or creates the person and ensures this product's grant. `enlace_activacion` appears once, only when the person cannot sign in yet |
| `PUT /servicio/accesos/{sub}` | -- | grants the caller's product |
| `DELETE /servicio/accesos/{sub}` | -- | revokes the caller's product; sends this product a `sub` logout |
| `GET /servicio/cuentas/{sub}` | -- | the current claims, for a product that needs a fresh name or email outside a sign-in |

* **A product calls `asegurar` BEFORE it creates a membership,** and revokes its own grant when the person's LAST
  membership in that product ends. The console never writes product grants directly (design 5.6).
* The activation link is delivered by **the product's own rule**: Lotes by hand, Tienda by email, Pedidos and
  Finanzas by hand until they mail. It lasts 7 days.
* `asegurar` with an email that already belongs to an active account returns that `sub` with `nueva: false` and no
  link. The person finds the new tenant at their next sign-in.
* **Errors** follow `admin-plane-v1.md`'s table: 401, 404, 422, and 409 with `type` one of `identificador_requerido`,
  `usuario_en_uso`, `cuenta_inactiva`.

## 10. Error codes a product must handle

| Where | Code | The product does |
|---|---|---|
| authorize redirect | `login_required` | start a visible sign-in (after `prompt=none`) |
| authorize redirect | `access_denied` | show «Sin acceso»: the person lacks this product's grant |
| authorize redirect | `invalid_request`, `unauthorized_client`, `server_error`, `temporarily_unavailable` | show the outage sentence in section 6 and record the code |
| token exchange | `invalid_grant` | restart the flow once, then show the outage sentence |
| any network failure to Cuenta | -- | the outage sentence. Never fall back to a local password |

Sign-in throttling answers `429 too_many_attempts` with `Retry-After`, at Cuenta. It throttles and never locks an
account, because with one account across four products a lock is a four-product denial of service.

## 11. Admin planes: the amendments this contract makes

1. **Every product's admin plane** gains `GET /admin/personas/{sub}/membresias`, which returns that product's
   memberships for the person (tenant id, tenant name, role, active). The console composes «Una persona en todos los
   productos» from Cuenta's record plus each product's answer. Cuenta never stores memberships.
2. **«Convertir» (conversion amendment 2.5)** stops minting its own invitation. It calls `asegurar` for the owner's
   email, links the returned `sub`, creates the membership, and returns Cuenta's activation link when `puede_entrar`
   is false.
3. **The acting admin is the staff `sub`.** `X-Duna-Admin-Id` carries the staff member's `sub` (opaque to every
   product, so no product changes), and the stability guarantee of section 4 replaces the one in the acting-admin
   amendment. The import publishes `vinculos-pedidos-admins.csv` (old `admin_id` to `sub`), so history written before
   the move stays attributable.
4. **duna-orders' `/admin` becomes an ordinary admin plane** (static bearer plus `X-Duna-Admin-Id`) like the other
   three. Its admin login, `AdminRow` and admin sessions are deleted in ORDERS-CUENTA-RETIRO-1.

Cuenta's own admin plane (`/admin/personas...`, design 7.1) follows `admin-plane-v1.md` exactly.

## 12. What retires in every product

Every password, forgot or set-password, invitation-token, reset-token and lockout path is **DELETED** in that product's
retirement slice before the deploy. None is hidden behind a flag, because a sign-in path that survives "temporarily" is
the shape that calcifies. Support sessions stay product-local: Cuenta offers no impersonation.

## 13. Irreversible at the first live use (on the deploy list)

* Issuer `https://cuenta.duna.solutions` and `sub` as an opaque UUID: changing either breaks every link.
* Passkey RP ID `cuenta.duna.solutions`: changing it invalidates every enrolled passkey. Passwords and recovery still
  work.
* The migration run: Carlos confirms the cross-product person list row by row first.

## 14. Changing this contract

A v1 change that a product could fail is a new version, `cuenta-duna-v2.md`, relayed to every owner before anything is
built against it. Adding a vector that encodes a rule already written here is not a version change, but it IS
relayed, because it can turn a green product red.
