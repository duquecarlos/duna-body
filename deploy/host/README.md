# Body app -- host deploy (duna-prod-1)

This directory holds Body's host-deploy-v1 deploy shape: a root-owned entry script, the systemd
units it installs, and the Caddy site for `body.duna.solutions`. Nothing in this directory ran on
the host while this was built -- it was written and tested as text plus `--dry-run`. Carlos
installs the entry script himself, and every deploy waits for his go.

## One-time install (as Carlos, with sudo)

    sudo install -o root -g root -m 755 deploy/host/duna-deploy-body /usr/local/sbin/duna-deploy-body
    sudo install -o root -g root -m 440 deploy/host/duna-deploy-body.sudoers /etc/sudoers.d/duna-deploy-body
    sudo visudo -c

This assumes the repository is already checked out, owned by Carlos's own host user, at
`/srv/duna/repos/duna-body`. `duna-deploy-body` creates the `duna-body` system user and
`/srv/duna/data/body` itself the first time it runs, and never deletes that directory.

Write `/srv/duna/env/body.env` by hand (owner root, group `duna-body`, mode 0640) from
`deploy/host/body.env.example` -- that file names every variable and how to generate it; no value
is ever committed to this repository.

Before the first deploy, Caddy needs somewhere to proxy to, so copy the site once by hand (the
host's main Caddyfile imports `/etc/caddy/sites/*.caddy`, confirmed by Main, 2026-11-06):

    sudo cp deploy/host/Caddyfile.body /etc/caddy/sites/body.caddy
    sudo systemctl reload caddy

Every later `deploy` installs/updates `Caddyfile.body` itself, so this copy is only needed once.

## Running it

    sudo -n /usr/local/sbin/duna-deploy-body status
    sudo -n /usr/local/sbin/duna-deploy-body deploy --dry-run
    sudo -n /usr/local/sbin/duna-deploy-body deploy

`deploy --dry-run` prints the exact command sequence and runs none of it -- no git, no systemctl,
no network. A real `deploy` pulls the repository as its own owner, then hands off (`exec`) to
`deploy/host/body-deploy.sh`, which builds the venv and installs the systemd units and the Caddy
site as root. After restarting the web service it checks `http://127.0.0.1:8770/healthz` (added by
BODY-CUENTA-RP-1); on any failure it rolls back to the commit that was deployed before the pull,
reinstalls, restarts, and exits non-zero naming the step that failed.

`body-pull.service` runs only the Garmin pull, not the shift import: shift files reach the host
through an uploader that is not built yet (BODY-SHIFT-UPLOAD-1).

## Self-updating wrapper (BODY-HOST-AUTOACTUALIZAR-1)

The wrapper updates itself on every `deploy`, right after the pull: it compares the freshly pulled
`deploy/host/duna-deploy-body` and `deploy/host/duna-deploy-body.sudoers` against what is installed
at `/usr/local/sbin/duna-deploy-body` and `/etc/sudoers.d/duna-deploy-body`. When both already
match, it prints `entry script: up to date` and goes straight on to `body-deploy.sh` as before.
When either changed, it validates the candidate sudoers file (exactly one
`<user> ALL=(root) NOPASSWD: /usr/local/sbin/duna-deploy-body` line) and runs `visudo -cf` on it --
either check failing refuses, with nothing installed. Otherwise it prints a diff of each changed
file, backs up the currently installed copies to `/usr/local/sbin/duna-deploy-body.prev` and
`/etc/sudoers.d/.duna-deploy-body.prev` (the leading dot keeps sudo from ever reading the backup as
a live rule), installs the new copies (mode 0755 for the script, 0440 for the sudoers rule), then
re-execs itself so the rest of that `deploy` runs under the new wrapper. This is the same mechanism
Lotes and Lugares already run.

`deploy --dry-run` shows the same plan without installing anything (`dry-run: entry script: up to
date`, or the diffs and `dry-run: sudoers check: would pass|would FAIL: <reason>`). If a bad
sudoers file ever ends up installed, `rollback-script` restores both files from their `.prev`
backups:

    sudo -n /usr/local/sbin/duna-deploy-body rollback-script

No hand reinstall is needed any more, for this or any later change to the wrapper: the very first
`deploy` after this change still runs under the OLD wrapper, so `body-deploy.sh` itself installs the
new wrapper and sudoers rule (through this same mechanism, as root, with no re-exec) before doing
anything else.

## Pull now (BODY-SYNC-VIVO-TIMER-1)

A relayed "pull now" uses the `pull` verb on `duna-deploy-body`. The wrapper updates itself on every
`deploy` (see "Self-updating wrapper" above), so no hand reinstall is needed to pick up this verb --
the first `deploy` after this change installed it through `body-deploy.sh`.

As Carlos himself or an orchestrator relaying his own go:

    sudo -n /usr/local/sbin/duna-deploy-body pull

This runs today's full Garmin pull right now -- `body-pull.service`, the exact job
`body-pull.timer` already runs at 09:30 America/Bogota, today plus its own healing window -- waits
for it to finish, then prints that run's own journal lines (`journalctl -u body-pull`, counts and
dates only, the job's own print lines) and exits with the service's own exit status.

### The on-demand sweep's own ceiling

`body-pull-hoy.timer` (ruling 2, this slice) sweeps today alone for every connected person every
30 minutes (`body-pull-hoy.service`, `python -m body.pull --hoy`); each person's own 06:00-23:00
daytime window is enforced in `body/pull.py`, not by the timer, so one timer safely covers several
zones. Inside that 17-hour window, the 30-minute timer can land for one person at most 34 times a
day (17 x 2). The live, in-app trigger (`pull_hoy_ahora`, BODY-SYNC-VIVO-2) can also fire inside
that same window, at most once every 15 minutes (`ESPACIO_MIN_HOY`) -- at most 68 times a day
(17 x 4, 60/15). Both write the SAME `garmin_cuenta.ultimo_intento_en` column, and this slice gives
the scheduled sweep that SAME 15-minute floor (never the scheduled pulls' own `espacio_min=0`), so
the two mechanisms share one clock instead of adding up: the combined daily ceiling for one
person, scheduled plus on-demand together, is the stricter bound that shared 15-minute floor sets
over the 17-hour window -- **at most ~68 "today"-pulls a day**. Each "today"-pull makes 7 Garmin
calls (the 6 feeds `garmin_adapter.fetch_day` reads, plus one read of the watch's own last-sync
instant), and a person inside a recorded backoff (2 hours after a 429, 15 minutes after a 5xx)
makes none of them until it expires -- so the ceiling this gives is ~68 x 7 = 476 Garmin calls a
day per person, a worst case the daytime window, the shared floor and any backoff together keep
the practical count well under.

## Off-host backup (BODY-HOST-BACKUP-1)

A daily timer runs `duna-deploy-body backup`, which execs `deploy/host/body-backup.sh` (no pull):
it takes a consistent copy of the database (`sqlite3.Connection.backup()`, never a plain file
copy), encrypts it with `age` to the public key in `BODY_BACKUP_AGE_RECIPIENT`, uploads it to the
`duna-respaldo` R2 bucket under `body/` with `curl --aws-sigv4`, and deletes every local plaintext
and encrypted copy (the plaintext deletion is in a `trap`, so it runs even on failure). R2
credentials reach `curl` only through its `-K -` stdin config, never as a command argument.

    sudo -n /usr/local/sbin/duna-deploy-body backup --dry-run
    sudo -n /usr/local/sbin/duna-deploy-body backup

If the host is missing the `age` CLI, `backup` refuses and names the exact command to run once:
`sudo apt install age` (same for `curl`, which Ubuntu 24.04 ships at 8.x with `--aws-sigv4` built in).

`status` reports each backup/pull/backfill job's own outcome, not just whether its timer is
active: `<unit> last run: <timestamp or never>`, and, once a job has run at least once, `<unit>
last result: success` or `<unit> last result: FAILED (<Result>, exit <N>)`. If the last backup run
failed, `status` itself exits 3, so a failing backup cannot read as healthy. To see why a backup
failed, read its journal: `journalctl -u body-backup.service -n 50 --no-pager`. The script prints
the object key and size on success (`body/body-<stamp>.sqlite.age size=<bytes>`); a run with no such
line in the journal did not upload. A failed upload now shows R2's own error reply (curl
`--fail-with-body`), including its `<Code>`/`<Message>`, e.g. `InvalidArgument`,
`SignatureDoesNotMatch`, `AccessDenied` or `NoSuchBucket` -- usually one of the `BODY_R2_*` env
values: `InvalidArgument` on the credential often means an API token was pasted where the R2 access
key ID belongs (a Cloudflare API token is a different length and shape than an R2 access key ID).

**Retention is a hand step, not code**: in the Cloudflare R2 dashboard, add a lifecycle rule on
bucket `duna-respaldo`, prefix `body/`, that deletes objects after 30 days.

**Restore check, once at the first deploy, on Carlos's PC** (where the `age` private key lives,
never on the server):

    # install age for Windows once: winget install FiloSottile.age
    # download the newest body/*.age object from the R2 dashboard, then:
    age -d -i duna-backup.key -o restore.sqlite <file>.age
    .venv\Scripts\python.exe -m body.backup check restore.sqlite
    # expect: ok, a table count, and per-table row counts -- never a value from any row
    del restore.sqlite

## Importar desde el PC (una vez) (BODY-MUDANZA-HOST-VERBO-1)

Moves one person's Body data from the PC's own `body.db` into this host, using the tool
BODY-MUDANZA-HERRAMIENTA-1 built (`python -m body.mudanza importar`). The snapshot travels by
`scp` over the same SSH access used for every other admin task here -- never `age`: its private
half lives on the PC, not on this host, so the host could never decrypt it. The wrapper already
carries these verbs -- it updates itself on every `deploy` (see "Self-updating wrapper" above), so
no hand reinstall is needed first. Carlos runs each step himself, in order:

1. On the PC, take a consistent snapshot (never the live file itself), then copy it to the host:

       .venv\Scripts\python.exe -m body.backup snapshot --out pc-snapshot.sqlite
       scp pc-snapshot.sqlite <host-user>@<host>:<staging-path>

2. Find the destination `sub` -- `personas` prints only a `cuenta_sub`, a display name and a
   first-consent date, never a health figure:

       sudo -n /usr/local/sbin/duna-deploy-body personas

3. Dry run -- prints the tool's per-table counts, writes nothing:

       sudo -n /usr/local/sbin/duna-deploy-body importar-pc --cuenta-sub <sub> --archivo <staging-path>

4. Apply -- takes a host backup first (the rollback point, printed, never uploaded), stops
   `body-pull.timer` and `body-backfill.timer` for the window, runs the import as `duna-body`,
   then restarts the timers and deletes the uploaded file, whether it succeeds or fails:

       sudo -n /usr/local/sbin/duna-deploy-body importar-pc --cuenta-sub <sub> --archivo <staging-path> --apply

5. Check the hosted app is healthy:

       curl -fsS https://body.duna.solutions/healthz

6. Reconnect Garmin on the hosted page -- the Garmin token never travels with the import
   (BODY-GARMIN-HOST-1): sign in again at `https://body.duna.solutions/garmin/conectar`.

**Rollback**: if step 5 fails, the timers are already restarted and the failure names the rollback
snapshot's path, printed just before the import ran. There is no restore command yet -- that is a
followup this slice did not build, not an oversight to paper over. Until one exists, restoring it
is a hand procedure: stop `body-web.service`, `body-pull.timer` and `body-backfill.timer`, copy
the rollback snapshot over `/srv/duna/data/body/body.db` as root, fix its ownership
(`duna-body:duna-body`) and mode (`0600`), then start the services again.

## Host facts, confirmed by Main (2026-11-06)

* the host is Ubuntu 24.04.4, so `python3` is 3.12
* the main Caddyfile imports `/etc/caddy/sites/*.caddy` -- the `.caddy` suffix is required
* the Caddy site is installed the way Lugares and Pagos do it (`install_caddy_site_if_changed`
  in `body-deploy.sh`): only if changed, the previous file kept as a `.bak`, restored (no reload)
  if `caddy validate` fails
* the repository owner is read at runtime with `stat -c %U`, never hardcoded
* `duna-body` is a system user created with its own group (`useradd --system --user-group ...`)
* `/srv/duna` itself is `duna:duna` mode 750, so `duna-body` cannot traverse into it by default;
  `body-deploy.sh` checks this before starting any unit and grants traverse-only access itself
  (`setfacl -m u:duna-body:x /srv/duna`, never recursive, never read or write) if the check fails,
  then checks again -- idempotent, and it stops naming the exact command if `setfacl` is missing
  or the grant still doesn't take

Ubuntu ships `python3-venv` separately from `python3`, so if the venv build step refuses with
"python3-venv is missing", run once, by hand:

    sudo apt install python3-venv

`deploy` checks for this itself and names the exact command if it's missing; it does not try a
second way to build the venv.

## What installs where

| File | Installed to | By |
| --- | --- | --- |
| `duna-deploy-body` | `/usr/local/sbin/duna-deploy-body` | Carlos, by hand, once; itself on every later `deploy` |
| `duna-deploy-body.sudoers` | `/etc/sudoers.d/duna-deploy-body` | Carlos, by hand, once; itself on every later `deploy` |
| `body-deploy.sh` | stays in the checkout | execed by `duna-deploy-body` |
| `body-web.service` | `/etc/systemd/system/body-web.service` | `body-deploy.sh` |
| `body-pull.service`, `body-pull.timer` | `/etc/systemd/system/` | `body-deploy.sh` |
| `body-backup.service`, `body-backup.timer` | `/etc/systemd/system/` | `body-deploy.sh` |
| `Caddyfile.body` | `/etc/caddy/sites/body.caddy` | `body-deploy.sh` (and Carlos, once, above) |

## Layout on the host

* `/srv/duna/repos/duna-body` -- the checkout, owned by Carlos's own host user. `.venv` lives
  inside it; the host is the only place `pip install` runs, and it always runs as that owner, not
  as root.
* `/srv/duna/data/body` (`BODY_HOME`) -- owned by the `duna-body` system user, mode 0700. The web
  and pull services run as that same user and only read the checkout. The weekly vida-v1 export
  (BODY-DISPOSITIVO-1) is no longer a host timer: it is built on demand, per person, by
  `GET /api/vida/semana`, so a leftover `body-vida.service`/`.timer` from an earlier deploy is
  stopped, disabled and removed by `body-deploy.sh` instead of installed.
* `/srv/duna/data/body/backup-tmp` -- scratch directory for `body-backup.sh`, mode 0700, owned by
  `duna-body`; it never holds anything once a backup run finishes (both the plaintext and the
  encrypted copy are deleted locally after the upload).
* `/srv/duna/env/body.env` -- root:duna-body, mode 0640, written by Carlos from
  `body.env.example`.
* The web service listens on `127.0.0.1:8770` only; Caddy is the only way in, over HTTPS, at
  `body.duna.solutions`. The back-channel path `/login/cuenta/logout-token` answers 404 at the
  Caddy layer too, so it is reachable only on loopback (where Cuenta Duna calls it).
