# Body app — design principles

Drawn on 2026-11-06 from Carlos's own rules across Duna (research in `docs/research/01–03`). Each rule says where it
comes from; the Body-specific reading is in *italics*. When a new rule arrives from the hub, add it here.

## The question each screen answers
| Screen | Question | Hero |
|---|---|---|
| **Hoy** | ¿Cómo llegas hoy y qué te toca? | Body Battery at wake + the shift ahead |
| **Turnos** | ¿Cuánto te cuesta una semana de noches y cuánto tardas en recuperarte? | ≈ recovery days after the last night block |
| **Tendencias** | ¿Vas mejorando? | each metric against *your own* 30-day baseline |
| **Metas** | ¿Tu plan está funcionando? | the weekly check, per goal |
| **Día** (drill-down) | ¿Qué pasó ese día? | sleep, shift, activity, every number of the day |

## Rules
1. **Data arrives by itself; you only confirm** (CARLOS-AUTO-INTAKE). *Garmin syncs each morning; shifts arrive from
   the Personal desk. Nothing is typed by hand except goals and a one-tap confirm.*
2. **Answer first, context one step away, never deleted** (Finanzas pulido). *One hero per screen; the rest opens.*
3. **Every figure opens what made it, with a way back** (Paridad #32, Lotes P2). *Resting HR → 30-day trend → the
   nights behind it → that day. Back always returns to the same spot (Paridad #30).*
4. **Measured ≠ estimated** (Lotes/Finanzas confianza). *Watch figures solid ink. Derived ones ("días para
   recuperarte", "costo de la semana") soft, rounded, with «≈» and "cómo lo calculo".*
5. **Your baseline, not a population norm.** *Every comparison is "vs. tu promedio de 30 días" or "vs. tus semanas
   de día". No judgement colours from generic tables.*
6. **Honest gaps** (legibilidad, owner brief). *No watch that night → «—» + "sin datos del reloj". Never 0, never a
   chart of zeros. Empty states say what will appear and when.*
7. **Shifts are the axis.** *Days are banded by shift kind (bar + tint + word: Día, Noche, Descanso), on every
   time chart.*
8. **Simple by default, detail on demand** (Sencillo/Detallado). *Hoy shows 4 figures; "Ver todo" opens the rest.
   Plan presets come from your history ("De tus datos"); a value you change is marked "Tú" with reset.*
9. **Charts explain themselves** (Finanzas). *Legend, scale, value on tap, "Toca un día…". One colour per series.
   Card titles are questions (Paridad #33).*
10. **Plain Spanish, tú** (house voice). *Cuenta register (with «¿ ¡»). No "HRV" without a plain subtitle the first
    time ("variabilidad del pulso: más alto suele ser mejor descanso"). Units on every figure.*
11. **Phone first** (375 px, 44 px targets, bottom tabs; left rail ≥ 1024). Hover only under `(hover:hover)`;
    visible pressed and focus states; reduced motion respected (ui-interaccion-v1).
12. **Never medical advice.** *The app shows patterns in your own numbers and the plan you chose. It does not
    diagnose. Abnormal readings point to "habla con tu médico", nothing more.*
13. **Health data stays home** (Body CLAUDE.md). *Local DB; no values in URLs or logs; no third-party analytics;
    mocks use invented demo data.*
14. **Assistant later, to agent-ui-v1** (one tap, 3 sizes, proposes → you confirm → undo). Built with parity: every
    screen ships with the assistant's read tool.

## Look
Duna family tokens (`docs/research/03`): paper #F4F2ED, card #FCFBF8, ink #121212, hairlines, Hanken Grotesk with
tabular numerals, no shadows, light + dark. Body's own marks (proposed, pending Carlos):
- product mark **#406F45** (calm green, Duna "ok") as the 8 px square / 3 px bar;
- shift bands: Noche **#3E5A73** (info blue-grey), Día amber tint **#FBE9C6** with amber bar, Descanso neutral inset.

## Round process (how Carlos designs)
Mock with realistic demo data → his notes → Se mantiene / Cambia / Nuevo list → build → he tests on his phone →
follow-ups become rules here → transferable ones go to the hub.
