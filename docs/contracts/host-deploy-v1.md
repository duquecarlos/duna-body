# host-deploy-v1 -- how a product is deployed by a session over SSH without a sudo password

Carlos, 2026-10-20, typed in Main, choosing option 2 of three (*"Clean long-term setup. Root-owned deploy scripts on the
server, and `sudo` without a password only for those scripts"*): *"2. And let's do it for Finanzas and lotes also."*
(ORCH-HOST-DEPLOY-V1). Measured the same evening: `sudo` on duna-prod-1 needs a password for `carlos`, so no session can
run a deploy step that uses it.

Every product that deploys to duna-prod-1 implements this shape. Same shape everywhere, so one sudoers rule pattern,
one way to call, one way to read the outcome.

## The shape

1. **An entry script per product, in the product repository:** `deploy/host/duna-deploy-<product>` (bash,
   `set -Eeuo pipefail`). `<product>` is lowercase, one word or hyphenated: `pedidos`, `cuenta-api`, `cuenta-ui`,
   `lugares`, `finanzas`, `lotes` (a product with two repositories may ship one entry per repository, or one that does
   both -- its orchestrator decides).
2. **Installed root-owned on the host, ONCE, by Carlos** (the only step that needs his password), from the repo's own
   `deploy/host/README.md`:

       sudo install -o root -g root -m 755 deploy/host/duna-deploy-<product> /usr/local/sbin/duna-deploy-<product>
       sudo install -o root -g root -m 440 deploy/host/duna-deploy-<product>.sudoers /etc/sudoers.d/duna-deploy-<product>
       sudo visudo -c

   The sudoers fragment is exactly one line: `carlos ALL=(root) NOPASSWD: /usr/local/sbin/duna-deploy-<product>`.
   Re-running the same two `install` lines is how an entry script is UPDATED -- a change to it needs Carlos again, which
   is the point: the thing that runs as root changes only by his hand. Keep the entry script THIN so it rarely changes.
3. **Called by a session as** `sudo -n /usr/local/sbin/duna-deploy-<product> <subcommand> [flags]`. `-n` means it fails
   instead of prompting.
4. **The entry script:**
   * refuses unless `EUID` is 0; resets `PATH` to a fixed value and does not trust the caller's environment;
   * pins its repository path as a constant (`/srv/duna/repos/<repo>`); takes NO free-form path or command from its
     arguments -- only a fixed list of subcommands and flags, anything else refused with a usage line, exit 2;
   * does the git pull / install / build AS THE REPOSITORY'S OWNER (`runuser -u <owner> --`, owner read with
     `stat -c %U`), never as root, so nothing in the checkout becomes root-owned;
   * pulls FIRST, and only then runs the freshly pulled repo code (never a script bash is still reading while the pull
     rewrites it -- `exec` a new process after the pull);
   * runs root only for what needs root: `systemctl`, `systemd-run` with `EnvironmentFile=/srv/duna/env/...`, Caddy,
     installing units;
   * NEVER prints, logs or copies an env file's contents, and never passes a secret in argv;
   * supports `--dry-run` (prints every command, runs none, needs no network);
   * exits 0 only when the deploy is healthy (its health check passed); non-zero with a message naming the failed step
     and whether the service was restarted.
5. **Trust boundary, stated:** the entry script runs repository code as root after pulling `main` from GitHub. So who can
   push to `main` on GitHub can run code as root on the host -- as today, where `carlos` runs the same scripts with sudo.
   Do not widen it: no subcommand that runs a path, a URL or a command given on the command line.

## Two traps found reviewing the first one (2026-10-20, CUENTA-UI-HOST-DEPLOY-1)

* **git as root on a checkout owned by another user FAILS** ("detected dubious ownership", git >= 2.35.2), and under
  `set -e` that kills the deploy at its first `git rev-parse`. So EVERY git call in the product's scripts, read-only ones
  included, runs as the owner (`runuser -u <owner> --` or `sudo -u <owner>`) -- never bare `git` once root runs it.
* **The rollback point must be the commit BEFORE the entry script's pull.** A product script that records
  "commit before" itself, after the entry already pulled, records the NEW commit. The entry captures the sha (as the
  owner) before pulling and passes it as `--before-sha <40 hex>`, which the product script validates and uses.

* **No `--pty` in anything a session calls.** Sessions call over `ssh -o BatchMode=yes` with no terminal; use
  `systemd-run --pipe --quiet --wait` (found reviewing CUENTA-API-HOST-DEPLOY-1).

* **`systemd-run` runs as the checkout's owner** (`--uid=`/`--gid=`), not root: Python run as root inside the
  checkout writes root-owned `__pycache__` that later breaks the owner's `uv sync`/`git` (ORDERS-HOST-DEPLOY-1 review).

## What stays by hand

The one-time install above, creating any env file (they hold secrets -- Carlos writes them), and anything the entry
script refuses. A product's one-off data step (a backfill, a seed) is a NAMED subcommand, with `--apply` separate from
its dry run, never "run this script".

## Tests the product repo carries

The argument allowlist (an unknown subcommand or a path is refused), the EUID refusal, `--dry-run` runs nothing, no
`echo`/`printf` of an env value, the sudoers fragment is the exact one line, and the install lines in
`deploy/host/README.md` match the file names.
