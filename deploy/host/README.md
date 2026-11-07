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

## Host facts, confirmed by Main (2026-11-06)

* the host is Ubuntu 24.04.4, so `python3` is 3.12
* the main Caddyfile imports `/etc/caddy/sites/*.caddy` -- the `.caddy` suffix is required
* the Caddy site is installed the way Lugares and Pagos do it (`install_caddy_site_if_changed`
  in `body-deploy.sh`): only if changed, the previous file kept as a `.bak`, restored (no reload)
  if `caddy validate` fails
* the repository owner is read at runtime with `stat -c %U`, never hardcoded
* `duna-body` is a system user created with its own group (`useradd --system --user-group ...`)

Ubuntu ships `python3-venv` separately from `python3`, so if the venv build step refuses with
"python3-venv is missing", run once, by hand:

    sudo apt install python3-venv

`deploy` checks for this itself and names the exact command if it's missing; it does not try a
second way to build the venv.

## What installs where

| File | Installed to | By |
| --- | --- | --- |
| `duna-deploy-body` | `/usr/local/sbin/duna-deploy-body` | Carlos, by hand |
| `duna-deploy-body.sudoers` | `/etc/sudoers.d/duna-deploy-body` | Carlos, by hand |
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
