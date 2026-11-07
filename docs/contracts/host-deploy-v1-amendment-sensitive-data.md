# host-deploy-v1 amendment — a product that keeps sensitive personal data on the host

Main wrote this on 2026-11-06 under ORCH-CARLOS-DELEGATES-TIER-1-1. Ledger: ORCH-HOST-SENSITIVE-DATA-1. Body asked for it
(BODY-DEPLOY-DUNA-1): it is the first product that keeps HEALTH data, and it keeps it in a file on duna-prod-1 (SQLite),
not in Neon. host-deploy-v1 covers how code gets deployed. It said nothing about the data the code keeps.

**Applies to** any product that keeps health, body, location-history or financial data about a person on the host's
disk. Body today. A product whose data lives only in Neon follows rules 4–7.

Colombian law (Ley 1581 de 2012) treats health data as *dato sensible*. Giving it is optional for the person, and they
must consent explicitly and know what is kept and why. Rule 6 is how we meet that.

## Rules

1. **Where it lives.** `/srv/duna/data/<product>/`, never inside the checkout (`/srv/duna/repos/...`). The directory is
   owned by the product's own system user, which no other product's service runs as. Mode 0700 for the directory and
   0600 for its files. The entry script creates it if it is missing and never deletes it.
2. **Access to an outside account (e.g. Garmin tokens)** is encrypted at rest (AES-256-GCM, as TIENDA-WOMPI-LLAVES-1
   does) with a key from the product's env file. Only the person signs in to the outside account, in the app's own
   page or by Carlos on the host. No session ever handles that password. Deleting a person deletes their token.
3. **Backups.** Hetzner server backups are on (CARLOS-HAND-STEPS row 25) and are the baseline. On top of that, the
   product ships a named entry-script subcommand, `backup`. It makes a consistent copy (for SQLite, `.backup`, never a
   file copy of a live database) and encrypts it to a PUBLIC key (age). It then uploads the copy off the host (the R2
   bucket `duna-respaldo`) and keeps 30 days. The private key never touches the server; Carlos keeps it. A restore is
   tested once, at the first deploy, on a scratch path. Until the bucket exists, the gap is written in the deploy
   walk-through, not assumed away.
4. **Per person.** Every row is keyed by the person's Cuenta `sub`. Every read filters by the signed-in `sub` on the
   server. A test proves that person B gets 404, not 403 and not the data, for person A's row. Sharing follows vida-v1:
   `visibility` defaults to `owner`, and a health figure is shared only when the person shares that figure. A shared
   household (hogar-v1) never opens health data by itself.
5. **It never leaves by a side door.** No health value goes into a log line, a URL or query string, an error page, a
   hub novedad, a worker report, or the admin plane. The console may know that the product is enabled for a person and
   nothing about their body. Request logs carry method, path template and status only.
6. **Consent, export, delete.** At first sign-in the person sees, in plain words, what is kept and why. They accept
   explicitly, and the acceptance is stored with its time and the text version. At any moment they can export
   everything (JSON in vida-v1 shapes) and delete everything. A deletion is immediate in the live data and ages out of
   the off-host backups within their 30 days, and the screen says so.
7. **Transport and session.** HTTPS through Caddy only, so the app's own port binds 127.0.0.1. Cookies are `Secure`,
   `HttpOnly`, `SameSite=Lax`. Sign-in goes through Cuenta Duna, with no local passwords.

## Tests the product repo carries (in addition to host-deploy-v1's)

The data path is outside the checkout, and the entry script never removes it. The 404 cross-person test is present. A
log-scrub test feeds a request carrying a health value and checks the value appears in no log output. `backup --dry-run`
prints the encrypt-and-upload steps and never a key. The export and delete routes exist and delete the token too.
