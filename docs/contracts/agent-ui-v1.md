# Duna assistant UI -- standard v1

Owner: Main (dev-protocol). Written 2026-10-23 from Carlos's own use of the products. Applies to EVERY Duna product
that has an in-app assistant: Pedidos (owner app), the admin console, Lotes, Finanzas, Tienda, and any later one.
The decisions below are Main's, under Carlos's delegation of UI-quality decisions (ORCH-CARLOS-DELEGATES-TIER-1-1).

**If you are an orchestrator or an agent who just received this file:** you do not need anyone to explain it. Read
section 1 (the six rules), then follow section 2 (how to adopt it in your own product). You decide HOW your product
meets each rule; the rules say WHAT the person must experience.

---

## Why this exists -- what Carlos saw

* On a phone in Lotes, nothing on screen leads directly to the assistant; it is behind «Menú».
* Lotes fills a wide monitor edge to edge; the admin console does not (it stops at 1240 px), and other apps vary.
* The assistant window cannot be made wider, maximized or moved.
* In Lotes, scrolling inside the assistant scrolled the whole app behind it (fixed there; very likely present in the
  others -- measured 2026-10-23: no `overscroll-behavior` in Finanzas, the console or the Pedidos owner app).
* In Finanzas, asked «dame el resumen ejecutivo del mes», the assistant first printed one row per internal step
  («Revisando el rango», «Revisando el dinero de las cuentas», «Sumando montos», «Resultado de la suma», «Sumando
  montos» ...), pushing the answer far below the question.

---

## 1. The six rules

### R1 -- The assistant is one tap away, on every screen, on a phone
A small, quiet, always-visible entry point (for example a round button at the bottom edge, or a slot in the bottom
navigation if the app has one). Never only inside «Menú». It must not cover the page's own primary actions or the
last row of content (add bottom padding to scrolling content equal to the button's height plus its margin).
It shows a subtle mark when the assistant has something unread or is still working.

### R2 -- The product uses the screen it is given
On wide screens the layout grows with the window: lists, tables, dashboards, maps and boards use the full width.
Only running prose (long paragraphs, forms with a single column of fields) keeps a readable measure (~70 characters
per line), centred inside the wide layout. No page-level `max-width` cap on the app shell itself. Nothing may scroll
sideways at 375 px.

### R3 -- The assistant has three sizes, not a free-floating window
* **Phone (< 768 px):** it always opens full screen, as a sheet, with a clear «Cerrar» / back gesture.
* **Computer:** a side panel (default ~400 px), a **wide** size (~50% of the window) and **full screen**, switched
  by one control in the panel header. The person can also drag the panel's inner edge to any width between the side
  and wide sizes. The chosen size is remembered per person on that device.
* While the panel is open at side or wide size, the page beside it stays usable and reflows (it is not covered).

*Why not a window you drag anywhere:* a floating window ends up on top of the very data the person is asking about,
and every product would invent its own window manager. Three sizes plus an adjustable edge give "wider" and "bigger"
with none of that.

### R4 -- Scrolling inside the assistant never moves the page behind it
The conversation's scroll container has `overscroll-behavior: contain`; on a phone the page behind the full-screen
sheet does not scroll while the sheet is open (scroll lock), and returns to exactly where it was when the sheet closes.

### R5 -- Work in progress is ONE line; the answer sits under the question
* While the assistant works, show **a single status line** under the person's message, with a gentle animation,
  whose text is replaced as each step happens («Revisando las cuentas…» -> «Sumando montos…»). Never one row per step.
* When the answer arrives, it appears **directly under the question**. The steps fold into one quiet, collapsed line
  above the answer -- «Cómo lo hice · 5 pasos» -- that opens to show them, for anyone who wants to check.
* Scrolling: when the answer arrives, bring the **start** of the answer into view (not the bottom of a long reply).
* Errors are one short sentence in the same place, with a retry, never a stack of failed steps.

### R6 -- Same habits everywhere in the conversation
Confirmation before any write stays exactly as each product already does it (the assistant proposes, the person
confirms in the conversation). Copy follows the product's own register (tuteo or usted). Long replies keep headings and
lists readable at 375 px; tables in a reply scroll inside themselves, never the page.

---

## 2. How to adopt it in your product (for the receiving orchestrator)

1. **Measure first, read-only.** For each rule R1-R6, state what your product does today, with file:line evidence,
   and test it in a real browser at 375 px and at a wide window (>= 1600 px). Mark each rule MEETS / PARTLY / MISSING.
2. **Decide and build.** Turn the PARTLY / MISSING rows into slices in your own queue, in your own order. Prefer one
   shared assistant component per product so the rules hold on every screen at once.
3. **Prove it in a browser**, not only in unit tests: phone width and wide window, panel at each size, a long reply,
   a multi-step answer, scrolling inside the panel.
4. **Report back.** If you are connected to the Duna hub, reply on the «Para productos» item that carried this file
   (`revisado` when measured, `adoptado` when built, `no_aplica` with the reason for any rule that cannot apply). If
   you are not connected yet, send the measurement table and the final commits to Carlos, who passes them to Main.

Edgar (Tienda): put this file in your repository (for example `docs/agent-ui-v1.md`) and tell your orchestrator:
«Adopta contracts/agent-ui-v1.md siguiendo la sección 2.» It will guide you from there.

---

## 3. How this travels -- the hub loop this file is the first example of

Duna's hub (`contracts/duna-hub-v1.md`) already has the mechanism Carlos asked for: Main posts ONE «Para productos»
item ("here is a fix or a standard; check your product"), targeted at the products it concerns. Each orchestrator
reads its «Para productos» items at session start, investigates its own product, builds what applies, and replies
`revisado` / `adoptado` / `no_aplica`. The console's «Por producto» view shows who has answered.

What is still needed before it runs by itself:
* the console's hub screens working live (the fix ADMIN-DUNA-HUB-WIRE-REAL-1 is in flight; then a console deploy);
* one key per orchestrator, minted by Carlos in the console (Hub > Llaves): `orquestador:main`, `orquestador:lotes`,
  `orquestador:finanzas`, and an agent key for Tienda (`agente:tienda`);
* a standing rule in each orchestrator's own governing file, which Carlos types in that orchestrator's window himself
  (a relayed approval to edit a peer's governing file is never accepted):
  > «Al iniciar cada sesión, lee tus ítems de "Para productos" en el hub, investiga tu producto por cada uno, y
  > respóndelos (revisado, adoptado o no_aplica) antes de despachar otro trabajo.»
