# Carlos's UI/UX principles from Lotes and Finanzas — research 2026-11-06

Read-only pass over Lotes/Finanzas DECISIONS, specs, design briefs, `finanzas/dev-protocol/CLAUDE.md` and the Lotes
per-repo memory files.

## Principles (evidence)
1. **Experience first.** Fewest steps, every state explained, every action's result visible, every figure traceable
   (finanzas/dev-protocol/CLAUDE.md 38–60). "Tests pass" ≠ good screen.
2. **Figures are links, never dead ends**; drill-down lands where the value can be corrected (memory
   lotes-figures-are-links; lotes-ui-principios-carlos-observe-1 P2).
3. **Pencil = edit, "add" is separate**; one edit surface per kind of record, reachable from anywhere (P1, P3).
4. **Answer first, context one step away, never deleted.** One hero figure per screen (04-finanzas-pulido brief).
5. **Round, readable numbers**: "≈179 kg", not 179.343; tabular numerals (memory estimates-read-round).
6. **Measured ≠ projected**: measured solid ink; projected softer, amber tint or «≈». Confidence labels
   (verificado / estimado / pendiente).
7. **Simple by default, detail on demand** (Sencillo/Detallado). Presets from his own history ("De tus lotes"),
   overridden values marked "Tú" with reset.
8. **Come back to the same spot** (scroll and open item restored).
9. **Every popup closes (X, Esc, outside) and every drill-down has a way back.** "Not being able to go back is a BUG".
10. **Charts explain themselves**: legend, scale, values on bars, "Toca una barra…", the right chart per figure.
11. **Honest "not yet"**: never a chart of twelve zeros; empty states say what will appear and when.
12. **Readable type, no wasted width**: body ≥ 14 px; no over-wide cards.
13. **Visible feedback for every state** (listening, thinking, noticeable hover).
14. **Assistant parity in the same batch**; never writes without approval; proposals undoable.
15. **Plain Spanish (Colombia)**: tuteo, no opening «¿», no trailing periods, no jargon; the user's own terms.
16. **Phone and desktop both first-class**; everything works at 375 px.
17. **Realistic demo data** in mocks.

## What he rejects
- Blank-page redesigns that lose existing capabilities; mock **on the existing screen**.
- Copying design pixels: take the style, merge into our structure.
- Charts he can't decode at a glance; mislabelled comparison periods; series that share one colour.
- Repeated notes on every card, small text, results buried at the bottom.
- Rulings asked without showing the screen (default answer "no").
- Invisible controls/hover. He wants sober, premium, not flashy.
- Turning a *mode* into a *kind of record*.
- Too many fixes at once: show the top of a ranked list.

## How a round goes
1. He dictates notes while using the live app (often phone/screenshot).
2. Read-only census of screens against the rule.
3. Choices as two concrete pictures with real values → his "yes".
4. Mock on the existing screen labelled **Se mantiene / Cambia / Nuevo** → "approved, go with the mock".
   Big passes: written design brief, three waves ranked by impact/effort.
5. Build; he tests the deployed result; follow-ups become rules.
6. Rule goes app-wide and to every product via the hub.

## House look (Lotes ui-v3 / Finanzas v3)
Hanken Grotesk, tabular numerals; ink #121212; paper #F4F3EF/#F4F2ED; card #FBFAF8/#FCFBF8; single amber accent
#F59E0B used sparingly; alert #B5482A; 1 px hairlines instead of shadows; radius 4–8 px; left rail on desktop, bottom
tab bar on phone. Calm off-white page, one hero figure + delta chip, 3–4 stat cards, grey/ink bar chart with a
highlighted current bar and tooltip, per-item list with thin bars.
