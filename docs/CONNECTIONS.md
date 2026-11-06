# Connections to other Duna apps

DESIGN rule 17: where another app holds the answer, link to it instead of copying it. No money in Body, no health
figures in other apps. Links carry date ranges only.

## Body → Finanzas (agreed with the Finanzas orchestrator, 2026-11-06)
- URL (exists today): `https://finanzas.duna.solutions/movimientos?desde=YYYY-MM-DD&hasta=YYYY-MM-DD&direccion=ingreso`
  - `desde` and `hasta` are both inclusive. Later filters: `q` (free text, e.g. the payroll concept) and
    `categoria=<id>` (per space; never hardcode).
- The link opens in whichever space his session is in; no space ever goes in the URL. His **Personal** space is being
  built (FIN-PERSONAL-INTAKE-1). **Don't ship the link until it exists**, or it opens Duna's company books.
- **Link to the payslip date, not the night week.** Extra hours are paid in a later payslip. Use the Personal desk's
  shift-to-payroll mapping (C:\Personal\enel\turnos) to find the pay date that covers a week, then link to that pay
  date's range. Don't make Finanzas guess.
- No pay-period screen exists yet; Movimientos filtered by date is where the link lands for now.

## Finanzas → Body
- Local now: `http://127.0.0.1:8770/day/YYYY-MM-DD`. Planned: `/shifts?from=YYYY-MM-DD&to=YYYY-MM-DD` (both
  inclusive). Hosted later on a Duna subdomain behind Cuenta; the host is to be confirmed.

## Lugares, Cuenta
No orchestrator of their own; go through Main. Household: see ROADMAP "Plan — Hogar".
