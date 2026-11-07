# Duna contracts, read-only copies

These seven files are read-only copies of contracts Main (the main orchestrator) owns at
`C:\Duna\dev-protocol\contracts\` (two of them, `hogar-v1.md` and `vida-v1.md`, say so themselves at FORK-1). **Main owns
the source. A change is made there and copied here (`BODY-CONTRATOS-1`'s copy step: `shutil.copyfile` per file, verified
byte for byte by sha256). Never edit a file in this directory directly** -- edit the source and re-copy.

Workers rooted in this repository read the relevant contract here before building to it, instead of relying on a spec's
restatement of it.

| File | What Body implements from it | sha256 |
|---|---|---|
| `agent-capabilities-v1.md` | The capability and write-confirmation rules behind Body's assistant: server-side proposals, confirm-in-chat before a write, the capability-tier vocabulary. | `2636338d1aee1a6783b490a315e7d98f21bbcdd4725ecf32635a4b9baff9cecc` |
| `agent-ui-v1.md` | The standard assistant UI (six rules) that `BODY-ASISTENTE-PANEL-1` built the assistant panel to, on every screen. | `bb40b06e3d171d62031c16b2e0173a94e8dd1279fcf64a857ba4bb63e690c9da` |
| `cuenta-duna-v1.md` | Sign-in through Cuenta Duna (`body/cuenta.py`) and who-is-signed-in (`body/identity.py`): identity is Cuenta's, authorization stays local to Body. | `7657ffeba2803d9a36a0e2537c56cf33615d47e68d9a75375f74deb83e1b29b4` |
| `hogar-v1.md` | Household plans and goals: Body reads the hogar claim from Cuenta sign-in (or the service API) once it has a Cuenta client, and keeps health figures private within the household unless a person shares one. | `54f74c133df0665328db3e4f54f871fa5819e93c94892d37dba615017c59233d` |
| `host-deploy-v1.md` | How Body's own `deploy/` scripts reach `duna-prod-1` over SSH: root-owned scripts, `sudo` without a password only for them. | `19ecacf85d340414c301c204b76441b82cbcf76a9431cc845da31c910b989813` |
| `host-deploy-v1-amendment-sensitive-data.md` | Body is the amendment's first case: health data kept in a SQLite file on the host (not Neon), never in a log/URL/hub item/worker report, with consent, export and delete at sign-in. | `902de086f0930a4beec5ec67260f085c855291dd02d18be0a567710ab40139e8` |
| `vida-v1.md` | The shared life-coach shapes (Event, Metric, Goal, `goal_check`) that Body's own `body/db.py` and weekly check adopted, with `body.*` namespaced metric keys and Garmin as a `source`. | `ff3354e1e10c4987570825fb41e60ac3f22fe76771998f884086e7b4a60dac7b` |
