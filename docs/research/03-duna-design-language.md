# Duna design language and how products plug in — research 2026-11-06

Sources: dev-protocol/research/design-admin-2026-10-06/duna-tokens.css, design-owner-2026-10-05/EXTRACT.md,
duna-cuenta-ui globals.css, brand/duna-brand-kit, contracts/{ui-interaccion-v1, legibilidad-v1, agent-ui-v1,
tutoriales-v1, nuestros-lugares-v1, host-deploy-v1}.md, dev-protocol DECISIONS.md.

## Tokens (shared family)
| Role | Light | Dark |
|---|---|---|
| paper | #F4F2ED | #161616 |
| card | #FCFBF8 | #1E1E1E |
| wash (row hover) | #F8F7F3 | |
| inset | #ECEAE4 | |
| line / soft | #C9C6BF / #E2DFD8 | |
| ink / 2 / muted | #121212 / #4A4844 / #6B6863 | #F2F1EC |
| amber (fill only, never text) | #F59E0B | |
| amber deep / tint | #7A4E00 / #FBE9C6 | #E0A33A |
| danger / warn / info / ok | #B3472A / #7A4E00 / #3E5A73 / #406F45 | |
| rail | #121212 | #121212 |

Product colours only as an 8 px square or 3 px bar (Pedidos #2F7F7C, Lotes #7A62A8, Finanzas #3F6FA8, Tienda
#A8577F, Duna #F59E0B). Heatmap ramp #EFECE5 → #B55A2E. No shadows: 1 px borders. Status = 3 px bar + tint + word.

Type: Hanken Grotesk, tabular numerals; JetBrains Mono for codes. Display 32–56/700 (44 phone), title 22–28,
figure 22–24/700, body 15/1.5, meta 13–14, chips 12.5–13/600 caps. Never < 12 px. 4 px spacing. Radius card 8–10,
controls 5–8, pills 999. Controls 36 px desktop / 44 px touch. < 768 tables→cards; < 1024 rail→top bar. Gutter 16 /
32 from 640. No shimmer. No emoji. Design at 375 first, then 1280/1440, light + dark.

Voice: Spanish (Colombia), tuteo, no English on screen. Register varies: Pedidos/kitchen drop «¿¡» (WhatsApp);
Cuenta keeps them. Copy changes are flagged, never silent.

## Plugging in
- **Cuenta Duna**: single sign-on (OIDC + passkeys), the only way in for hosted products. Each product registers a client.
- **Hub**: `hub.py check` at session start; replies adoptado / revisado / no_aplica. "body" not in product list yet.
- **Assistant**: Lotes is the reference (agent-ui-v1: one tap on phone, 3 sizes, «Cómo lo hice · N pasos», propose →
  confirm, undo).
- **Hosting**: Hetzner duna-prod-1, systemd + Caddy, subdomain, host-deploy-v1, security checklist from slice one.
- **Closest precedent: Nuestros lugares**, a personal app: Next.js PWA, Cuenta sign-in, SQLite, its own visual
  identity (from a Claude Design handoff) while keeping house rules.

## Personal-level Duna
No "Duna humano/vida" document. Emerging set: Personal desk (C:\Personal), Finanzas Personal space
(FIN-PERSONAL-INTAKE-1, entity type `hogar`), Lugares, Body.
