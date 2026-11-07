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

Before the first deploy, Caddy needs somewhere to proxy to, so copy the site once by hand (this
assumes the host's main Caddyfile imports `/etc/caddy/sites/*` -- to confirm with Main):

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

`backup` is reserved for BODY-HOST-BACKUP-1 and is not implemented yet.

## What installs where

| File | Installed to | By |
| --- | --- | --- |
| `duna-deploy-body` | `/usr/local/sbin/duna-deploy-body` | Carlos, by hand |
| `duna-deploy-body.sudoers` | `/etc/sudoers.d/duna-deploy-body` | Carlos, by hand |
| `body-deploy.sh` | stays in the checkout | execed by `duna-deploy-body` |
| `body-web.service` | `/etc/systemd/system/body-web.service` | `body-deploy.sh` |
| `body-pull.service`, `body-pull.timer` | `/etc/systemd/system/` | `body-deploy.sh` |
| `body-vida.service`, `body-vida.timer` | `/etc/systemd/system/` | `body-deploy.sh` |
| `Caddyfile.body` | `/etc/caddy/sites/body.caddy` | `body-deploy.sh` (and Carlos, once, above) |

## Layout on the host

* `/srv/duna/repos/duna-body` -- the checkout, owned by Carlos's own host user. `.venv` lives
  inside it; the host is the only place `pip install` runs, and it always runs as that owner, not
  as root.
* `/srv/duna/data/body` (`BODY_HOME`) -- owned by the `duna-body` system user, mode 0700. The web,
  pull and vida services run as that same user and only read the checkout.
* `/srv/duna/env/body.env` -- root:duna-body, mode 0640, written by Carlos from
  `body.env.example`.
* The web service listens on `127.0.0.1:8770` only; Caddy is the only way in, over HTTPS, at
  `body.duna.solutions`. The back-channel path `/login/cuenta/logout-token` answers 404 at the
  Caddy layer too, so it is reachable only on loopback (where Cuenta Duna calls it).
