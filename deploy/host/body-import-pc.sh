#!/usr/bin/env bash
# body-import-pc.sh -- moves one person's Body data from their own PC into this host
# (BODY-MUDANZA-HOST-VERBO-1, following BODY-MUDANZA-SERVIDOR-1-INV's design and
# BODY-MUDANZA-HERRAMIENTA-1's tool). Called by duna-deploy-body importar-pc/personas; runs as
# root, the rollback snapshot and the import itself as duna-body via runuser, exactly as
# body-backup.sh runs its own snapshot step.
#
# diagnostico-personas/relinkar-garmin (BODY-GARMIN-PERSONA-1) are also dispatched here, beside
# personas -- same data user, same BODY_HOME. relinkar-garmin --apply runs the EXISTING off-host
# backup (body-backup.sh, not this script's own local rollback snapshot) first and refuses to run
# the move at all if that backup fails.
#
# detalle-historia (BODY-DETALLE-HISTORIA-1) is also dispatched here: the manual, paced backfill
# over one person's whole activity-detail history, forwarded to body.detalle_historia -- same
# data user, same BODY_HOME, same shape as cobertura's own arm.
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "body-import-pc.sh: refused -- must run as root (EUID 0)" >&2
  exit 1
fi

PATH="/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

REPO_DIR="/srv/duna/repos/duna-body"
DATA_USER="duna-body"
DATA_DIR="/srv/duna/data/body"

if [[ $# -lt 1 ]]; then
  echo "body-import-pc.sh: usage: body-import-pc.sh <importar-pc|personas|diagnostico-personas|cobertura|detalle-historia|relinkar-garmin> [options]" >&2
  exit 2
fi

action="$1"
shift

case "$action" in
  personas)
    # BODY-HISTORIA-MINUTO-2 ruling 3: --conteo is the ONE extra argument this arm accepts --
    # the standing-deploy-go census, two counts only. Anything else, including any other
    # argument, keeps the exact original refusal below; the plain (no-argument) mode is
    # unchanged.
    if [[ $# -eq 1 && "$1" == "--conteo" ]]; then
      exec runuser -u "$DATA_USER" -- env -C "$REPO_DIR" "BODY_HOME=$DATA_DIR" \
        "$REPO_DIR/.venv/bin/python" -m body.cobertura --conteo
    fi
    if [[ $# -ne 0 ]]; then
      echo "body-import-pc.sh: personas takes no arguments" >&2
      exit 2
    fi
    exec runuser -u "$DATA_USER" -- env -C "$REPO_DIR" "BODY_HOME=$DATA_DIR" \
      "$REPO_DIR/.venv/bin/python" -m body.mudanza personas
    ;;
  diagnostico-personas)
    if [[ $# -ne 0 ]]; then
      echo "body-import-pc.sh: diagnostico-personas takes no arguments" >&2
      exit 2
    fi
    exec runuser -u "$DATA_USER" -- env -C "$REPO_DIR" "BODY_HOME=$DATA_DIR" \
      "$REPO_DIR/.venv/bin/python" -m body.persona_garmin diagnostico
    ;;
  cobertura)
    COBERTURA_SUB=""
    COBERTURA_JSON=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --cuenta-sub)
          COBERTURA_SUB="$2"
          shift 2
          ;;
        --json)
          COBERTURA_JSON="1"
          shift
          ;;
        *)
          echo "body-import-pc.sh: unknown argument: $1" >&2
          exit 2
          ;;
      esac
    done
    if [[ -z "$COBERTURA_SUB" ]]; then
      echo "body-import-pc.sh: cobertura requires --cuenta-sub" >&2
      exit 2
    fi
    if [[ -n "$COBERTURA_JSON" ]]; then
      exec runuser -u "$DATA_USER" -- env -C "$REPO_DIR" "BODY_HOME=$DATA_DIR" \
        "$REPO_DIR/.venv/bin/python" -m body.cobertura --cuenta-sub "$COBERTURA_SUB" --json
    fi
    exec runuser -u "$DATA_USER" -- env -C "$REPO_DIR" "BODY_HOME=$DATA_DIR" \
      "$REPO_DIR/.venv/bin/python" -m body.cobertura --cuenta-sub "$COBERTURA_SUB"
    ;;
  detalle-historia)
    DETALLE_HISTORIA_SUB=""
    DETALLE_HISTORIA_DRY_RUN=""
    DETALLE_HISTORIA_MAX=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --cuenta-sub)
          DETALLE_HISTORIA_SUB="$2"
          shift 2
          ;;
        --dry-run)
          DETALLE_HISTORIA_DRY_RUN="1"
          shift
          ;;
        --max)
          DETALLE_HISTORIA_MAX="$2"
          shift 2
          ;;
        *)
          echo "body-import-pc.sh: unknown argument: $1" >&2
          exit 2
          ;;
      esac
    done
    if [[ -z "$DETALLE_HISTORIA_SUB" ]]; then
      echo "body-import-pc.sh: detalle-historia requires --cuenta-sub" >&2
      exit 2
    fi
    DETALLE_HISTORIA_ARGS=(--cuenta-sub "$DETALLE_HISTORIA_SUB")
    if [[ -n "$DETALLE_HISTORIA_DRY_RUN" ]]; then
      DETALLE_HISTORIA_ARGS+=(--dry-run)
    fi
    if [[ -n "$DETALLE_HISTORIA_MAX" ]]; then
      DETALLE_HISTORIA_ARGS+=(--max "$DETALLE_HISTORIA_MAX")
    fi
    # BODY-DETALLE-HISTORIA-1-B: runuser alone never loaded /srv/duna/env/body.env, so the first
    # live run stopped on a missing BODY_TOKEN_KEY. A transient unit carrying the SAME properties
    # as body-pull.service (EnvironmentFile, uid, BODY_HOME, working directory, hardening) cannot
    # drift from the scheduled pull's own environment -- never sourced, cat'ed or printed.
    if ! command -v systemd-run >/dev/null 2>&1; then
      echo "body-import-pc.sh: refused -- systemd-run not found; detalle-historia requires the same environment as the scheduled pull" >&2
      exit 1
    fi
    exec systemd-run --wait --pipe --collect --quiet \
      --uid="$DATA_USER" \
      --property=EnvironmentFile=/srv/duna/env/body.env \
      --setenv="BODY_HOME=$DATA_DIR" \
      --working-directory="$REPO_DIR" \
      --property=NoNewPrivileges=yes \
      --property=ProtectSystem=strict \
      --property=ProtectHome=yes \
      --property=ReadWritePaths="$DATA_DIR" \
      --property=PrivateTmp=yes \
      --property=UMask=0077 \
      "$REPO_DIR/.venv/bin/python" -m body.detalle_historia "${DETALLE_HISTORIA_ARGS[@]}"
    ;;
  importar-pc)
    ;;
  relinkar-garmin)
    ;;
  *)
    echo "body-import-pc.sh: unknown action: $action" >&2
    exit 2
    ;;
esac

if [[ "$action" == "relinkar-garmin" ]]; then
  RELINKAR_DESDE=""
  RELINKAR_HACIA=""
  RELINKAR_APPLY=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --desde)
        RELINKAR_DESDE="$2"
        shift 2
        ;;
      --hacia)
        RELINKAR_HACIA="$2"
        shift 2
        ;;
      --apply)
        RELINKAR_APPLY="1"
        shift
        ;;
      *)
        echo "body-import-pc.sh: unknown argument: $1" >&2
        exit 2
        ;;
    esac
  done

  if [[ -z "$RELINKAR_DESDE" || -z "$RELINKAR_HACIA" ]]; then
    echo "body-import-pc.sh: --desde and --hacia are required" >&2
    exit 2
  fi

  if [[ -z "$RELINKAR_APPLY" ]]; then
    # Dry run: the tool's own dry run. No host backup, no timers touched.
    exec runuser -u "$DATA_USER" -- env -C "$REPO_DIR" "BODY_HOME=$DATA_DIR" \
      "$REPO_DIR/.venv/bin/python" -m body.persona_garmin relinkar \
      --desde "$RELINKAR_DESDE" --hacia "$RELINKAR_HACIA"
  fi

  # --apply (BODY-GARMIN-PERSONA-1 ruling 4): the EXISTING off-host backup script runs first,
  # and the move does not run at all if it fails -- this is Body's own body-backup.sh, the same
  # encrypt-and-upload-to-R2 job the daily timer runs, not importar-pc's own local rollback
  # snapshot above (that snapshot is this script's own convention for importar-pc alone).
  if ! /bin/bash "$REPO_DIR/deploy/host/body-backup.sh"; then
    echo "body-import-pc.sh: refused -- the host backup failed; relinkar-garmin --apply did not run" >&2
    exit 1
  fi

  exec runuser -u "$DATA_USER" -- env -C "$REPO_DIR" "BODY_HOME=$DATA_DIR" \
    "$REPO_DIR/.venv/bin/python" -m body.persona_garmin relinkar \
    --desde "$RELINKAR_DESDE" --hacia "$RELINKAR_HACIA" --apply
fi

SUB=""
ARCHIVO=""
APPLY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cuenta-sub)
      SUB="$2"
      shift 2
      ;;
    --archivo)
      ARCHIVO="$2"
      shift 2
      ;;
    --apply)
      APPLY="1"
      shift
      ;;
    *)
      echo "body-import-pc.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$SUB" || -z "$ARCHIVO" ]]; then
  echo "body-import-pc.sh: --cuenta-sub and --archivo are required" >&2
  exit 2
fi

if [[ ! -f "$ARCHIVO" ]]; then
  echo "body-import-pc.sh: file not found: $ARCHIVO" >&2
  exit 1
fi

if [[ -z "$APPLY" ]]; then
  # Dry run: the tool's own dry run. No integrity check, no host backup, no timers touched, and
  # the uploaded file is never deleted here -- the file may still be worth looking at.
  exec runuser -u "$DATA_USER" -- env -C "$REPO_DIR" "BODY_HOME=$DATA_DIR" \
    "$REPO_DIR/.venv/bin/python" -m body.mudanza importar --origen "$ARCHIVO" --cuenta-sub "$SUB"
fi

# --apply from here, in this order (BODY-MUDANZA-HOST-VERBO-1 ruling 2):
#   (a) integrity_check on the uploaded file -- refuse before touching anything else
#   (b) a host backup, with the EXISTING snapshot step, kept locally as the rollback point --
#       never uploaded by this verb, and never deleted: it is the rollback path printed on failure
#   (c) stop body-pull.timer and body-backfill.timer for the window, restarted in a trap on every
#       exit path, exactly as body-backup.sh deletes its plaintext in a trap on every exit path
#   (d) run the import itself as the duna-body data user
#   (e) its own report, already on stdout
#   (f) delete the uploaded file -- also in the trap, so a failure never leaves it behind

integrity="$(python3 -c '
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("PRAGMA integrity_check").fetchone()[0])
' "$ARCHIVO")"
if [[ "$integrity" != "ok" ]]; then
  echo "body-import-pc.sh: refused -- integrity_check on the uploaded file: $integrity" >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
rollback_file="$DATA_DIR/rollback-$timestamp.sqlite"

runuser -u "$DATA_USER" -- env -C "$REPO_DIR" "BODY_HOME=$DATA_DIR" \
  "$REPO_DIR/.venv/bin/python" -m body.backup snapshot --out "$rollback_file"
echo "rollback snapshot: $rollback_file"

cleanup_uploaded() {
  systemctl restart body-pull.timer body-backfill.timer
  rm -f "$ARCHIVO"
}
trap cleanup_uploaded EXIT

systemctl stop body-pull.timer body-backfill.timer

chown "$DATA_USER:$DATA_USER" "$ARCHIVO"

runuser -u "$DATA_USER" -- env -C "$REPO_DIR" "BODY_HOME=$DATA_DIR" \
  "$REPO_DIR/.venv/bin/python" -m body.mudanza importar --origen "$ARCHIVO" --cuenta-sub "$SUB" --apply
