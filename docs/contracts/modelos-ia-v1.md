# modelos-ia-v1: the one set of AI models every Duna assistant uses

Decided by Main 2026-11-07 on Carlos's ask: *"make a standard of what model will be used, check the difference and solve
them."* Measured that day: three products ran three different sets (Lotes on the standard below; Finanzas and Lugares on
`gpt-5.1` / `gemini-2.5-flash`; Orders' and Lugares' code defaults on `claude-sonnet-4-5`).

## The standard (assistants: the chat agent inside each product)

| Provider | Model | US cents per million tokens, in / out (cached in) |
|---|---|---|
| Anthropic | `claude-sonnet-5-5` | 200 / 1000 (20) |
| OpenAI | `gpt-6.1-sol` | 200 / 1000 (10) |
| Gemini | `gemini-3.8-flash` | 75 / 375 until 2026-12-31, then 150 / 750 (cached: not known, price it at full input) |

**v1.1, 2026-11-07 (asked by Body):** gpt-6.1-sol cached input 10 (OpenAI's published price, seen by Growth
2026-10-04). Gemini 3.8 Flash's cached price is not on any record: only one aggregator gives a figure, and that same
source gives half the input price everyone else gives, so it is not used. Until Google's own page is read, cached
input is priced at full input, which overstates spend and trips a cap early, never late.

Default provider: `anthropic`. Prices from Growth's `research/cost-scaling.md` and the Orders and Lotes price lines
already set on the host (CARLOS-HAND-STEPS.md, 2026-10-04).

**The same model locally and on the server.** A local key differs from a production key only in its spend container,
never in its model, so a test run measures what production will run.

## Not covered: small fixed tasks

Voice-note transcription and receipt OCR (Orders: `gemini-3.1-flash-lite`) and judges inside eval harnesses are a
cheaper tier chosen per task. They stay as they are and are named here so nobody "standardises" them by accident.

**The WhatsApp order engine** (Orders' `LLM_MODEL`, today `claude-sonnet-4-5`) moves to `claude-sonnet-5-5` only
through a measured variation round, because its behaviour is evaluated persona by persona. It is not an assistant.

## Where it lands

- Every product's code default for an assistant model is the standard, so an unset `*_MODELO` / `*_MODEL` line runs it.
  Where a product requires the line (Finanzas, Lotes, Body: key AND model, or the provider is off), its env example
  carries the standard as the value.
- Every price map (`OWNER_AGENT_MODEL_PRICES_USD_MINOR`, `LOTES_AGENTE_PRECIOS`, `FINANZAS_AGENTE_PRECIOS`, Lugares'
  `precio.ts`, Body's cap) names these three models.
- A change to the standard is a new version of this file and a «Para productos» hub item the same turn.
