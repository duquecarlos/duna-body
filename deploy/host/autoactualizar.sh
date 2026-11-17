#!/usr/bin/env bash
# autoactualizar.sh -- shared self-update helper for Body's host wrapper (BODY-HOST-AUTOACTUALIZAR-1).
# Sourced from the freshly pulled checkout by duna-deploy-body (after its own pull) and by
# body-deploy.sh (at bootstrap) -- never executed on its own, so it declares no PATH and no EUID
# check; both callers already set those.

INSTALLED_SCRIPT_PATH="/usr/local/sbin/duna-deploy-body"
INSTALLED_SUDOERS_PATH="/etc/sudoers.d/duna-deploy-body"
PREV_SCRIPT_PATH="/usr/local/sbin/duna-deploy-body.prev"
PREV_SUDOERS_PATH="/etc/sudoers.d/.duna-deploy-body.prev"

# sudo ignores any file in sudoers.d whose name contains a dot, so PREV_SUDOERS_PATH's leading dot
# keeps this backup from ever being read as a live rule.

validar_sudoers_candidato() {
  local candidate="$1"
  local filtered
  filtered="$(sed 's/\r$//' "$candidate" | grep -v '^[[:space:]]*#' | grep -v '^[[:space:]]*$' || true)"
  local count
  count="$(printf '%s\n' "$filtered" | grep -c . || true)"
  if [[ "$count" -ne 1 ]]; then
    echo "expected exactly one rule line after stripping blank and comment lines, found $count"
    return 1
  fi
  if ! printf '%s\n' "$filtered" | grep -Eq "^[a-z_][a-z0-9_-]{0,31} ALL=\(root\) NOPASSWD: ${INSTALLED_SCRIPT_PATH}\$"; then
    echo "rule line does not match the required shape: $filtered"
    return 1
  fi
  return 0
}

autoactualizar_comparar() {
  local repo_script="$1"
  local repo_sudoers="$2"
  SCRIPT_CHANGED=""
  SUDOERS_CHANGED=""
  if ! cmp -s "$repo_script" "$INSTALLED_SCRIPT_PATH" 2>/dev/null; then
    SCRIPT_CHANGED="1"
  fi
  if ! cmp -s "$repo_sudoers" "$INSTALLED_SUDOERS_PATH" 2>/dev/null; then
    SUDOERS_CHANGED="1"
  fi
}

autoactualizar_plan() {
  local repo_script="$1"
  local repo_sudoers="$2"
  autoactualizar_comparar "$repo_script" "$repo_sudoers"
  if [[ -z "$SCRIPT_CHANGED" && -z "$SUDOERS_CHANGED" ]]; then
    echo "dry-run: entry script: up to date"
    return 0
  fi
  if [[ -n "$SCRIPT_CHANGED" ]]; then
    echo "dry-run: entry script changed:"
    if [[ -f "$INSTALLED_SCRIPT_PATH" ]]; then
      diff -u "$INSTALLED_SCRIPT_PATH" "$repo_script" || true
    else
      echo "(no installed copy yet)"
    fi
  fi
  if [[ -n "$SUDOERS_CHANGED" ]]; then
    echo "dry-run: sudoers file changed:"
    if [[ -f "$INSTALLED_SUDOERS_PATH" ]]; then
      diff -u "$INSTALLED_SUDOERS_PATH" "$repo_sudoers" || true
    else
      echo "(no installed copy yet)"
    fi
    local reason
    if reason="$(validar_sudoers_candidato "$repo_sudoers")" && visudo -cf "$repo_sudoers" >/dev/null 2>&1; then
      echo "dry-run: sudoers check: would pass"
    else
      if [[ -z "$reason" ]]; then
        reason="visudo -cf rejected the candidate sudoers file"
      fi
      echo "dry-run: sudoers check: would FAIL: $reason"
    fi
  fi
  return 0
}

autoactualizar_instalar() {
  local repo_script="$1"
  local repo_sudoers="$2"
  autoactualizar_comparar "$repo_script" "$repo_sudoers"
  if [[ -z "$SCRIPT_CHANGED" && -z "$SUDOERS_CHANGED" ]]; then
    echo "entry script: up to date"
    return 0
  fi
  if [[ -n "$SUDOERS_CHANGED" ]]; then
    local reason
    if ! reason="$(validar_sudoers_candidato "$repo_sudoers")"; then
      echo "autoactualizar: refused -- candidate sudoers file invalid: $reason" >&2
      exit 1
    fi
    if ! visudo -cf "$repo_sudoers" >/dev/null 2>&1; then
      echo "autoactualizar: refused -- visudo -cf rejected the candidate sudoers file" >&2
      exit 1
    fi
  fi
  if [[ -n "$SCRIPT_CHANGED" ]]; then
    echo "entry script changed:"
    if [[ -f "$INSTALLED_SCRIPT_PATH" ]]; then
      diff -u "$INSTALLED_SCRIPT_PATH" "$repo_script" || true
    else
      echo "(no installed copy yet)"
    fi
  fi
  if [[ -n "$SUDOERS_CHANGED" ]]; then
    echo "sudoers file changed:"
    if [[ -f "$INSTALLED_SUDOERS_PATH" ]]; then
      diff -u "$INSTALLED_SUDOERS_PATH" "$repo_sudoers" || true
    else
      echo "(no installed copy yet)"
    fi
  fi
  if [[ -n "$SCRIPT_CHANGED" && -f "$INSTALLED_SCRIPT_PATH" ]]; then
    install -o root -g root -m 0440 "$INSTALLED_SCRIPT_PATH" "$PREV_SCRIPT_PATH"
  fi
  if [[ -n "$SUDOERS_CHANGED" && -f "$INSTALLED_SUDOERS_PATH" ]]; then
    install -o root -g root -m 0440 "$INSTALLED_SUDOERS_PATH" "$PREV_SUDOERS_PATH"
  fi
  if [[ -n "$SCRIPT_CHANGED" ]]; then
    install -o root -g root -m 0755 "$repo_script" "$INSTALLED_SCRIPT_PATH"
  fi
  if [[ -n "$SUDOERS_CHANGED" ]]; then
    install -o root -g root -m 0440 "$repo_sudoers" "$INSTALLED_SUDOERS_PATH"
  fi
  return 0
}
