# Ready to post: Body round 1 → every product (para-productos)

Post once Pedidos is deployed with the "body" vocabulary and Carlos has minted the `body` caller key:

    python C:/Duna/dev-protocol/scripts/hub.py --config <body hub config> para-productos --product body \
      --targets pedidos,lotes,finanzas,tienda,cuenta,consola,pagos,lugares,duna \
      --ref BODY-DESIGN-R1 --title "Body round 1: transferable principles" --body-file docs/hub/round-1-para-productos.md

(If `--body-file` does not exist, paste the section below as `--body`.)

---

Body (Carlos's health app, new product) shipped its first design round on 2026-11-06. Source: `C:\Duna\body-app\docs\DESIGN.md`
(17 rules), mock `design/mocks/v1/`. Principles that can travel, written for any product:

1. **Our own estimate as the hero, and it shows its work.** When a screen's main number is computed by us, show «≈», round it, add a
   band word, and a "cómo lo calculo" view that lists each part with its own number and weight. With too little data, say so instead
   of guessing. Version the formula so a change never silently rewrites past values.
   *Analogies:* Finanzas projected balance / monthly result; Lotes stock forecast; Pedidos delivery ETA; Tienda margin.
2. **Compare with the person's own history, not a generic norm.** "vs. your last 30 days" / "vs. your whole history", with the normal
   range (p25–p75) drawn as a band behind the line. *Analogies:* Finanzas spending vs your usual month; Pedidos volume vs a normal week;
   Lotes turnover vs the product's own range.
3. **A "typical week" table** (average per weekday, lowest day marked, only when the spread is meaningful) answers "which day costs me?"
   in one look. *Analogies:* Pedidos orders per weekday; Tienda sales per weekday; Finanzas spending per weekday.
4. **Long history in one fixed-height chart:** monthly means as a line, labels as HTML under the chart (never stretched SVG text),
   stroke that keeps its thickness at any width. Passes at 375 px without sideways scroll.
5. **Honest gaps:** no data → «—» and why, never 0 and never a chart of zeros.
6. **Card titles are questions** ("Are you sleeping more or less than before?"), not metric names.
7. **Two languages from day one:** every string in one dictionary per language (es-CO default, en full), language as a person setting;
   no text written inside a screen.
8. **Link out where another app holds the answer** instead of copying its numbers (Body night-shift week → its pay in Finanzas), and
   expose a small summary other apps can read.
9. **Sensitive data stays home:** values never in URLs or logs; jobs print counts, not values; tests are isolated so they can never
   touch the real database (Body learned this the hard way).

Each orchestrator decides what applies; please reply adoptado / revisado / no_aplica.
