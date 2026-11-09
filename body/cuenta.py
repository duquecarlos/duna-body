"""Cuenta Duna sign-in (cuenta-duna-v1): OIDC authorization code + PKCE S256, ID-token and
back-channel logout-token validation, server-side sessions.

BODY_CUENTA_ISSUER set -> hosted mode: Body signs people in through Cuenta Duna. Unset -> local
mode: every function here that touches the network or a cookie is unreachable (body/web.py's
routes 404 and body/identity.py never calls into this module).

All network goes through `_get_json` and `_post_form` below -- the only two functions tests patch
to run with no network. Never store an ID token or a password; only a sha256 of the session token
and of the `flujo` cookie value ever reach the database (body/db.py's `sesion` / `inicio_sesion`).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote, urlencode

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from . import household

ISSUER_ENV = "BODY_CUENTA_ISSUER"
PUBLIC_URL_ENV = "BODY_PUBLIC_URL"
CLIENT_SECRET_ENV = "BODY_CUENTA_CLIENT_SECRET"
CLIENT_ID = "body"
SWITCHER_URL = "https://cuenta.duna.solutions/cuenta"

SESSION_COOKIE = "body_sesion"
FLUJO_COOKIE = "flujo"

ATTEMPT_TTL_MINUTES = 10
SESSION_IDLE_DAYS = 14
SESSION_ABSOLUTE_DAYS = 30
LAST_SEEN_UPDATE_SECONDS = 3600
MARGEN_SEGUNDOS = 60
ACR_MINIMO = "pwd"
JWKS_REFETCH_SECONDS = 60

_EVENTO_LOGOUT = "http://schemas.openid.net/event/backchannel-logout"
_ACR_RANK = {"pwd": 0, "passkey": 1}
_HTTP_TIMEOUT = 5.0

_discovery_cache: dict = {}
_jwks_cache: dict = {"keys": None, "fetched_at": 0.0}


class CuentaUnavailable(Exception):
    """Cuenta could not be reached, or answered with an error, during a network step."""


class FlowRefused(Exception):
    """The sign-in attempt or callback was refused; `codigo` names which cuenta-duna-v1 check
    failed (or 'flujo' / 'code' / 'error' for Body's own pre-token-exchange checks)."""

    def __init__(self, codigo: str):
        super().__init__(codigo)
        self.codigo = codigo


@dataclass
class CallbackResult:
    kind: str  # "session" | "sin_acceso" | "reauth"
    return_to: str | None = None
    session_token: str | None = None
    claims: dict | None = None


# ---- configuration -----------------------------------------------------------------------------

def hosted() -> bool:
    return bool(os.environ.get(ISSUER_ENV))


def ensure_configured() -> None:
    """Called once, from body/web.py's lifespan, before the app accepts any request. Hosted mode
    with a missing variable refuses to start; the message never carries a value."""
    if not hosted():
        return
    for name in (PUBLIC_URL_ENV, CLIENT_SECRET_ENV):
        if not os.environ.get(name):
            raise RuntimeError(f"{name} is required when {ISSUER_ENV} is set")


def issuer() -> str:
    return os.environ[ISSUER_ENV]


def public_url() -> str:
    return os.environ[PUBLIC_URL_ENV].rstrip("/")


def client_secret() -> str:
    return os.environ[CLIENT_SECRET_ENV]


def _redirect_uri() -> str:
    return public_url() + "/login/cuenta/callback"


def _post_logout_uri() -> str:
    return public_url() + "/login"


def _secure_cookies() -> bool:
    return public_url().startswith("https://")


def safe_return_to(raw: str | None) -> str:
    """"/" alone, or "/" followed by a character that is neither "/" nor "\\" -- and nowhere in
    the value a "\\", a control character, or a "://" scheme separator. A browser reads a
    backslash as a forward slash, so "/\\evil.example" (which starts with "/", not "//") is a
    protocol-relative redirect off this site exactly like "//evil.example" (BODY-SEGURIDAD-1)."""
    if not raw:
        return "/"
    if raw == "/":
        return "/"
    if not raw.startswith("/") or raw[1] in ("/", "\\"):
        return "/"
    if "\\" in raw or "://" in raw:
        return "/"
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in raw):
        return "/"
    return raw


# ---- small helpers ------------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _random_token(n: int = 32) -> str:
    return _b64url(secrets.token_bytes(n))


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _auth_time_iso(valor) -> str | None:
    """The verified ID token's `auth_time` (epoch seconds) as ISO UTC, or None when it is absent
    or not an integer -- a session with no `auth_time` is STALE, never treated as "now"."""
    if isinstance(valor, bool) or not isinstance(valor, int):
        return None
    try:
        return datetime.fromtimestamp(valor, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


# ---- network seam (patched in tests) -------------------------------------------------------------

def _get_json(url: str, auth: tuple[str, str] | None = None) -> dict:
    with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
        r = c.get(url, auth=auth)
        r.raise_for_status()
        return r.json()


def _post_form(url: str, data: dict, auth: tuple[str, str]) -> httpx.Response:
    with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
        return c.post(url, data=data, auth=auth)


def _reset_caches_for_tests() -> None:
    _discovery_cache.clear()
    _jwks_cache["keys"] = None
    _jwks_cache["fetched_at"] = 0.0


def _discovery() -> dict:
    doc = _discovery_cache.get("doc")
    if doc is not None:
        return doc
    try:
        doc = _get_json(f"{issuer()}/.well-known/openid-configuration")
    except httpx.HTTPError as e:
        raise CuentaUnavailable() from e
    _discovery_cache["doc"] = doc
    return doc


def _fetch_jwks_keys() -> list[dict]:
    doc = _discovery()
    try:
        return _get_json(doc["jwks_uri"]).get("keys", [])
    except httpx.HTTPError as e:
        raise CuentaUnavailable() from e


def _resolve_key_for_validation(kid: str | None):
    now = time.monotonic()
    if _jwks_cache["keys"] is not None:
        key = _find_key(_jwks_cache["keys"], kid)
        if key is not None:
            return key
    if _jwks_cache["keys"] is None or now - _jwks_cache["fetched_at"] >= JWKS_REFETCH_SECONDS:
        fresh = _fetch_jwks_keys()
        _jwks_cache["keys"] = fresh
        _jwks_cache["fetched_at"] = now
        return _find_key(fresh, kid)
    return None


def _exchange_code(code: str, code_verifier: str) -> dict:
    try:
        r = _post_form(
            f"{issuer()}/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(),
                "code_verifier": code_verifier,
            },
            (CLIENT_ID, client_secret()),
        )
    except httpx.HTTPError as e:
        raise CuentaUnavailable() from e
    if r.status_code != 200:
        raise CuentaUnavailable()
    try:
        body = r.json()
    except ValueError as e:
        raise CuentaUnavailable() from e
    if "id_token" not in body:
        raise CuentaUnavailable()
    return body


# ---- id_token / logout_token validation (pure; no network) ---------------------------------------

def _find_key(keys: list[dict], kid: str | None):
    for k in keys:
        if k.get("kid") == kid:
            return RSAAlgorithm.from_jwk(json.dumps(k))
    return None


def _acr_satisfies(acr: str | None, minimo: str | None) -> bool:
    if not minimo:
        return True
    if acr in _ACR_RANK and minimo in _ACR_RANK:
        return _ACR_RANK[acr] >= _ACR_RANK[minimo]
    return acr == minimo


def _verificar_claims(token: str, *, client_id: str, issuer_: str, ahora: int, margen: int, tipo: str,
                       find_key: Callable[[str | None], object | None],
                       nonce_esperado: str | None = None,
                       requiere_acr: str | None = None,
                       jti_vistos: set | None = None) -> tuple[dict | None, str | None]:
    """Mirrors dev-protocol's contracts/cuenta-duna-v1/verificar_vectores.py exactly for every
    check it makes, with one deliberate difference: `acr` is compared by RANK (pwd < passkey), as
    the contract's prose asks ("below the client minimum as a RANK"), not by exact string equality
    -- which would wrongly refuse a passkey sign-in to a client whose minimum is `pwd`. Every
    vector in tests/fixtures/cuenta_vectores.json passes either way, since none of them exercises
    an acr strictly above its vector's minimum."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.DecodeError:
        return None, "firma"
    if header.get("alg") != "RS256":
        return None, "alg"
    key = find_key(header.get("kid"))
    if key is None:
        return None, "kid"
    if tipo == "logout_token" and header.get("typ") != "logout+jwt":
        return None, "typ"
    try:
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=client_id, issuer=issuer_, leeway=margen,
            options={"verify_iat": False, "require": ["exp", "iat"]},
        )
    except jwt.InvalidAudienceError:
        return None, "aud"
    except jwt.InvalidIssuerError:
        return None, "iss"
    except jwt.ExpiredSignatureError:
        return None, "exp"
    except (jwt.InvalidSignatureError, jwt.DecodeError):
        return None, "firma"
    if isinstance(claims.get("aud"), list):
        return None, "aud"
    if claims["iat"] > ahora + margen:
        return None, "iat"
    if tipo == "id_token":
        if not claims.get("sub"):
            return None, "sub"
        if claims.get("nonce") != nonce_esperado:
            return None, "nonce"
        if requiere_acr and not _acr_satisfies(claims.get("acr"), requiere_acr):
            return None, "acr"
    else:
        if "nonce" in claims:
            return None, "nonce"
        if not isinstance(claims.get("events"), dict) or _EVENTO_LOGOUT not in claims["events"]:
            return None, "events"
        if not claims.get("sub") and not claims.get("sid"):
            return None, "sub_o_sid"
        if jti_vistos is not None:
            if claims.get("jti") in jti_vistos:
                return None, "jti"
            jti_vistos.add(claims.get("jti"))
    return claims, None


def verificar_claims(token: str, *, client_id: str, issuer: str, ahora: int, margen: int, tipo: str,
                      jwks: dict, jwks_tras_refetch: dict | None = None,
                      nonce_esperado: str | None = None,
                      requiere_acr: str | None = None,
                      jti_vistos: set | None = None) -> tuple[dict | None, str | None]:
    """Pure entry point for tests/test_cuenta_vectores.py: `jwks` / `jwks_tras_refetch` are the
    vector's own static key sets (no network, no process cache). `jti_vistos`, if given, is a set
    the caller threads across calls -- the vectors file itself expects a replayed `jti` to be
    refused this way (its `logout-jti-repetido` case). Production back-channel logout does NOT use
    this: it checks the durable `logout_jti` table instead (see handle_backchannel_logout)."""
    def find_key(kid):
        key = _find_key(jwks.get("keys", []), kid)
        if key is not None:
            return key
        if jwks_tras_refetch is not None:
            return _find_key(jwks_tras_refetch.get("keys", []), kid)
        return None

    return _verificar_claims(token, client_id=client_id, issuer_=issuer, ahora=ahora, margen=margen,
                              tipo=tipo, find_key=find_key, nonce_esperado=nonce_esperado,
                              requiere_acr=requiere_acr, jti_vistos=jti_vistos)


# ---- sign-in attempts (`inicio_sesion`) -----------------------------------------------------------

def _sweep_expired_attempts(conn, now_iso: str) -> None:
    cutoff = (datetime.fromisoformat(now_iso) - timedelta(minutes=ATTEMPT_TTL_MINUTES)).isoformat(
        timespec="seconds"
    )
    conn.execute("DELETE FROM inicio_sesion WHERE created_at < ?", (cutoff,))


def start_login(conn, return_to: str | None, prompt: str | None = None,
                 max_age: int | None = None) -> tuple[str, str]:
    """Stores a new sign-in attempt and returns (authorize_url, flujo_cookie_value)."""
    now = _now_iso()
    _sweep_expired_attempts(conn, now)
    flujo_id = _random_token()
    state = _random_token()
    nonce = _random_token()
    verifier = _random_token(48)
    conn.execute(
        "INSERT INTO inicio_sesion (flujo_hash, state, nonce, code_verifier, return_to, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_sha256_hex(flujo_id), state, nonce, verifier, return_to, now),
    )
    conn.commit()
    params = {
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": _redirect_uri(),
        "scope": "openid", "state": state, "nonce": nonce,
        "code_challenge": _pkce_challenge(verifier), "code_challenge_method": "S256",
    }
    if prompt in ("none", "login"):
        params["prompt"] = prompt
    if max_age is not None:
        params["max_age"] = max_age
    url = f"{issuer()}/oauth/authorize?{urlencode(params)}"
    return url, flujo_id


def handle_callback(conn, flujo_cookie: str | None, params: dict, language: str = "es") -> CallbackResult:
    now = _now_iso()
    _sweep_expired_attempts(conn, now)
    if not flujo_cookie:
        raise FlowRefused("flujo")
    row = conn.execute(
        "SELECT * FROM inicio_sesion WHERE flujo_hash = ?", (_sha256_hex(flujo_cookie),)
    ).fetchone()
    if not row:
        raise FlowRefused("flujo")
    # single use: consume immediately, whatever happens next
    conn.execute("DELETE FROM inicio_sesion WHERE flujo_hash = ?", (row["flujo_hash"],))
    conn.commit()
    created = datetime.fromisoformat(row["created_at"])
    if datetime.now(timezone.utc) - created > timedelta(minutes=ATTEMPT_TTL_MINUTES):
        raise FlowRefused("flujo")
    if params.get("error") == "login_required":
        return CallbackResult(kind="reauth", return_to=row["return_to"])
    if params.get("state") != row["state"]:
        raise FlowRefused("state")
    error = params.get("error")
    if error == "access_denied":
        return CallbackResult(kind="sin_acceso")
    if error in ("invalid_request", "unauthorized_client", "server_error", "temporarily_unavailable"):
        # cuenta-duna-v1 section 10: these four authorize-redirect codes show the outage
        # sentence, same as a network failure reaching Cuenta.
        raise CuentaUnavailable()
    if error:
        # cuenta-duna-v1 section 10 lists only the codes above; any other or unknown code
        # keeps today's acceso_rechazado behaviour.
        raise FlowRefused("error")
    code = params.get("code")
    if not code:
        raise FlowRefused("code")
    token_response = _exchange_code(code, row["code_verifier"])
    claims, codigo = _verificar_claims(
        token_response["id_token"], client_id=CLIENT_ID, issuer_=issuer(), ahora=int(time.time()),
        margen=MARGEN_SEGUNDOS, tipo="id_token", find_key=_resolve_key_for_validation,
        nonce_esperado=row["nonce"], requiere_acr=ACR_MINIMO,
    )
    if codigo:
        raise FlowRefused(codigo)
    person_id = resolve_person(conn, claims, language)
    if person_id is None:
        return CallbackResult(kind="sin_acceso", claims=claims)
    _sincronizar_hogar_sin_fallar(conn, person_id, claims)
    token = create_session(conn, person_id, claims.get("sid"), _auth_time_iso(claims.get("auth_time")))
    return CallbackResult(kind="session", return_to=row["return_to"], session_token=token)


# ---- linking (`person.cuenta_sub`) ----------------------------------------------------------------

def resolve_person(conn, claims: dict, language: str = "es") -> int | None:
    """By `sub` ONLY -- never by email. None means «Sin acceso»: `body` is not in `duna_productos`
    and no person is already linked to this sub. `language` seeds the new row's `language` column
    on FIRST sign-in only (picked by the caller from the callback request's Accept-Language header,
    through body/i18n's `pick` -- Cuenta's ID token and userinfo carry no locale claim,
    BODY-CUENTA-1-INV part B); an existing person's language is never touched here."""
    sub = claims["sub"]
    row = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (sub,)).fetchone()
    if row:
        return row["id"]
    if "body" not in (claims.get("duna_productos") or []):
        return None
    name = claims.get("name") or "Cuenta"
    cur = conn.execute(
        "INSERT INTO person (display_name, cuenta_sub, language) VALUES (?, ?, ?)", (name, sub, language)
    )
    conn.commit()
    return cur.lastrowid


# ---- household mirror (`hogar_miembro`, hogar-v1) -------------------------------------------------

def _sincronizar_hogar_sin_fallar(conn, person_id: int, claims: dict) -> None:
    """Calls household.sincronizar_desde_claim and swallows any failure -- sign-in must succeed
    whether or not the household mirror could be updated. Logs a count-only line (no sub, no hogar
    id)."""
    try:
        household.sincronizar_desde_claim(conn, person_id, claims)
    except Exception as e:
        print(f"cuenta: hogar sync failed for 1 person ({type(e).__name__})")


def refrescar_hogar(conn, person_id: int) -> None:
    """GET /servicio/cuentas/{sub} (cuenta-duna-v1 section 9, `sub` percent-escaped into the path),
    client_secret_basic, and re-syncs the household mirror from the fresh claims -- only when the
    answer parses as a JSON object. Any error -- network, a non-2xx answer, bad JSON, an answer
    that is not a dict, or a failure inside sincronizar_desde_claim -- leaves the existing mirror
    untouched and logs a count-only line; nothing here may ever raise into its caller."""
    row = conn.execute("SELECT cuenta_sub FROM person WHERE id = ?", (person_id,)).fetchone()
    if not row or not row["cuenta_sub"]:
        return
    try:
        claims = _get_json(
            f"{issuer()}/servicio/cuentas/{quote(row['cuenta_sub'], safe='')}",
            auth=(CLIENT_ID, client_secret()),
        )
    except (httpx.HTTPError, ValueError) as e:
        print(f"refrescar_hogar: network error for 1 person ({type(e).__name__})")
        return
    if not isinstance(claims, dict):
        print("refrescar_hogar: non-dict answer for 1 person")
        return
    _sincronizar_hogar_sin_fallar(conn, person_id, claims)


def _debe_refrescar_hogar(conn, person_id: int) -> bool:
    """True when this person has no `hogar_miembro` row, or their 'cuenta' row is older than
    LAST_SEEN_UPDATE_SECONDS (one hour). A 'local' row (demo/tests) is never refreshed from Cuenta."""
    row = conn.execute(
        "SELECT fuente, actualizado_at FROM hogar_miembro WHERE person_id = ?", (person_id,)
    ).fetchone()
    if row is None:
        return True
    if row["fuente"] != "cuenta":
        return False
    actualizado = datetime.fromisoformat(row["actualizado_at"])
    return (datetime.now(timezone.utc) - actualizado).total_seconds() >= LAST_SEEN_UPDATE_SECONDS


# ---- sessions (`sesion`) ---------------------------------------------------------------------------

def create_session(conn, person_id: int, cuenta_sid: str | None, auth_time: str | None = None) -> str:
    token = _random_token(32)
    now = datetime.now(timezone.utc)
    now_s = now.isoformat(timespec="seconds")
    expires_s = (now + timedelta(days=SESSION_ABSOLUTE_DAYS)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO sesion (id_hash, person_id, cuenta_sid, auth_time, created_at, last_seen_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_sha256_hex(token), person_id, cuenta_sid, auth_time, now_s, now_s, expires_s),
    )
    conn.commit()
    return token


def resolve_session(conn, token: str | None, now: datetime | None = None) -> tuple[int | None, bool]:
    """(person_id, existed). `existed` tells the caller whether a session row was ever found for
    this cookie -- even an expired one -- so it can choose a silent `prompt=none` retry over a
    plain sign-in page."""
    if not token:
        return None, False
    now = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT * FROM sesion WHERE id_hash = ?", (_sha256_hex(token),)).fetchone()
    if not row:
        return None, False
    if now >= datetime.fromisoformat(row["expires_at"]):
        return None, True
    last_seen = datetime.fromisoformat(row["last_seen_at"])
    if now - last_seen > timedelta(days=SESSION_IDLE_DAYS):
        return None, True
    if (now - last_seen).total_seconds() >= LAST_SEEN_UPDATE_SECONDS:
        conn.execute(
            "UPDATE sesion SET last_seen_at = ? WHERE id_hash = ?",
            (now.isoformat(timespec="seconds"), row["id_hash"]),
        )
        conn.commit()
        if hosted() and _debe_refrescar_hogar(conn, row["person_id"]):
            try:
                refrescar_hogar(conn, row["person_id"])
            except Exception as e:
                print(f"resolve_session: hogar refresh failed for 1 person ({type(e).__name__})")
    return row["person_id"], True


def reciente(conn, session_token_or_row, segundos: int = 600) -> bool:
    """True only when the session's `auth_time` is present and within `segundos` of now -- the
    sensitive-data re-authentication gate (ORCH-HOST-SENSITIVE-DATA-1 part 3). A session with no
    `auth_time` (pre-migration, or a token that never carried one) is STALE, never treated as
    recent. Takes either a session token (looked up fresh) or an already-fetched `sesion` row."""
    if session_token_or_row is None:
        return False
    if isinstance(session_token_or_row, str):
        row = conn.execute(
            "SELECT auth_time FROM sesion WHERE id_hash = ?", (_sha256_hex(session_token_or_row),)
        ).fetchone()
    else:
        row = session_token_or_row
    if not row or not row["auth_time"]:
        return False
    auth_time = datetime.fromisoformat(row["auth_time"])
    age = (datetime.now(timezone.utc) - auth_time).total_seconds()
    return -MARGEN_SEGUNDOS <= age <= segundos


def end_session(conn, token: str | None) -> str | None:
    """Ends the LOCAL session (if any) and returns its stored Cuenta `sid`, for `logout_hint`."""
    if not token:
        return None
    h = _sha256_hex(token)
    row = conn.execute("SELECT cuenta_sid FROM sesion WHERE id_hash = ?", (h,)).fetchone()
    conn.execute("DELETE FROM sesion WHERE id_hash = ?", (h,))
    conn.commit()
    return row["cuenta_sid"] if row else None


def logout_url(sid: str | None) -> str:
    params = {"client_id": CLIENT_ID, "post_logout_redirect_uri": _post_logout_uri()}
    if sid:
        params["logout_hint"] = sid
    return f"{issuer()}/oauth/logout?{urlencode(params)}"


# ---- back-channel logout ---------------------------------------------------------------------------

def _sweep_expired_jtis(conn, now_ts: int) -> None:
    conn.execute("DELETE FROM logout_jti WHERE exp < ?", (now_ts,))


def jti_seen(conn, jti: str) -> bool:
    return conn.execute("SELECT 1 FROM logout_jti WHERE jti = ?", (jti,)).fetchone() is not None


def mark_jti_seen(conn, jti: str, exp: int) -> None:
    conn.execute("INSERT OR IGNORE INTO logout_jti (jti, exp) VALUES (?, ?)", (jti, exp))


def handle_backchannel_logout(conn, token: str) -> bool:
    """True: the token was valid (sessions ended, or none matched). False: refused (400)."""
    now_ts = int(time.time())
    claims, codigo = _verificar_claims(
        token, client_id=CLIENT_ID, issuer_=issuer(), ahora=now_ts, margen=MARGEN_SEGUNDOS,
        tipo="logout_token", find_key=_resolve_key_for_validation,
    )
    if codigo:
        return False
    jti = claims.get("jti")
    if not jti:
        return False
    _sweep_expired_jtis(conn, now_ts)
    if jti_seen(conn, jti):
        return False
    sid, sub = claims.get("sid"), claims.get("sub")
    if sid:
        conn.execute("DELETE FROM sesion WHERE cuenta_sid = ?", (sid,))
    else:
        conn.execute(
            "DELETE FROM sesion WHERE person_id IN (SELECT id FROM person WHERE cuenta_sub = ?)", (sub,)
        )
    mark_jti_seen(conn, jti, int(claims["exp"]))
    conn.commit()
    return True


# ---- cookies ------------------------------------------------------------------------------------

def set_flujo_cookie(response, flujo_id: str) -> None:
    response.set_cookie(FLUJO_COOKIE, flujo_id, max_age=ATTEMPT_TTL_MINUTES * 60, path="/",
                         httponly=True, samesite="lax", secure=_secure_cookies())


def clear_flujo_cookie(response) -> None:
    response.delete_cookie(FLUJO_COOKIE, path="/")


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_ABSOLUTE_DAYS * 86400, path="/",
                         httponly=True, samesite="lax", secure=_secure_cookies())


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
