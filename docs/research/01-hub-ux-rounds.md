# Hub UX rounds, shift data, "Duna personal" — research 2026-11-06

Read-only pass over the hub (`hub.py items --kind para_productos`, `show`) and C:\Personal. 95 para_productos items;
~35 are UX/UI principles (the rest: security, payments, bug fixes).

## UX principle rounds that apply to Body

| Round | Origin | Principle (transferable) |
|---|---|---|
| CARLOS-AUTO-INTAKE (2026-11-06) | Main | Never retype what a system already sent you; ingest automatically, ask only to confirm/classify. |
| Interruptor (mode switch) | Lotes | A variant of an action is a mode switch on the same screen, not a new screen; button says what it saves. |
| Two-tap | Lotes | −/+ counters with live result, prefilled values, one save + one undo for the batch. |
| Consent in place | Lotes | Consent asked inline where needed, versioned. |
| Ronda 2: crear en sitio | Lotes | Every picker's last option creates in place; empty states say what to do next. |
| Ronda sin fricción | Lotes | Top actions visible with icons (not under "Más"); values set once, prefilled after. |
| Every UX idea travels | Lotes/Main | Meta rule: rounds go to every product via the hub. |
| tutoriales-v1 | Main | One task per tutorial, < 90 s, "in the app" and "with the assistant". |
| legibilidad-v1 (8) | Main | Names not ids; plain subtitle per screen; attention first; readable charts; no empty rows; everything that names something opens; visible hover; every figure has unit + scope. |
| ui-interaccion-v1 (9) + amendments | Main | Hover only with pointer; pressed state everywhere; visible focus; nothing hover-only; disabled says why; reduced motion; one style source; hover contrast measured; everything clickable opens. |
| agent-ui-v1 (6) | Main | Assistant one tap away on phone, 3 remembered sizes, never scrolls the page, one progress line "Cómo lo hice · N pasos", confirms before writing. |
| Paridad #25–35 | Lotes | Voice states; dark mode (light default, no flash); Ctrl/Cmd+K search; return restores scroll/open item; popups close by X/Esc/outside; every figure drills to its source with a way back; honest analytics («≈», empty cards say why, card titles are questions); read errors say "no se pudo cargar". |

## Top 10 repeated ideas
1. Everything that names something opens; every number drills to its source, with a way back.
2. No dead ends: empty states say why and link to the fix.
3. Create in place.
4. Readable for a non-engineer: plain words, unit + scope on every figure.
5. Honest numbers: «≈» on estimates, reasons for gaps, card titles as questions.
6. Data arrives by itself; the person only confirms.
7. Fewer taps: prefill, −/+, one save, one undo.
8. Assistant has parity with screens; confirms before writing.
9. Visible, measured interaction states; mobile-first at 375 px.
10. Attention first; returning restores where you were.

## Shift data (C:\Personal\enel\turnos\FORMATO.md — written for Body's `shift` table)
- Personal desk extracts each "TURNOS <MES> A <MES>" mail (xlsx) once into `turnos-<YYYY-MM>_<YYYY-MM>.json`;
  newest `received` wins per day.
- Shape: `source{mail_subject, received, attachment, extracted_at}`, `person`, `timezone: America/Bogota`,
  `shifts[]{day, kind, code, start_local, end_local, holiday, sunday, note}`. `kind` matches Body's CHECK list.
- Code→kind/hours mapping unknown until the first real schedule → `codigos.json`. No JSON files yet.
- Body imports the JSON files locally with `source = personal:<file>`; never reads mail.
- **Timezone is America/Bogota**, not Europe/Madrid (fix Body's person default).

## "Duna personal"
No formal product yet. Pieces: C:\Personal (assistant across job, company, health); Main's auto-intake round puts
personal finances as a "Personal" space on the Finanzas engine; Body app is "template, maybe product later".
Emerging pattern: a Personal space in each Duna engine, fed by the Personal desk.
