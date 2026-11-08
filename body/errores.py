"""Body's one rule for exception text that leaves the process (terminal, log, tool result):
the TYPE name only, never the message, unless the exception is one of Body's OWN classes
(defined under the `body` package) whose message Body itself composed.

Why: garminconnect builds several of its own sign-in messages out of a lower exception's raw
text, and a third-party message -- the library's, the filesystem's, a parser's, the network's
-- is not ours to vouch for. body/garmin_adapter.py's `_client` already followed this rule
before this module existed; tests/test_errores_texto.py's AST guard fails any other spot in
body/**/*.py that renders the name bound by an `except ... as <name>` clause directly through an
f-string, `.format()`, `str()`, `repr()` or `%` -- route that site through one of the two
functions below instead (BODY-LOGIN-MENSAJES-1).
"""
from __future__ import annotations


def texto_seguro(exc: BaseException) -> str:
    """The exception's TYPE name only -- never its message. Safe for ANY exception, Body's own
    or a library's, wherever the caller cannot vouch for what the message contains."""
    return type(exc).__name__


def texto_propio(exc: BaseException) -> str:
    """The message of one of Body's OWN exception classes (its type defined in a module under
    `body`), whose text Body itself composed and already decided is safe to show or log. Using
    this on anything else is the mistake the guard exists to catch; this is the runtime half of
    that check, for the cases the static scan cannot see (an exception instance passed in from
    elsewhere, or one the scan cannot resolve)."""
    modulo = type(exc).__module__
    if modulo != "body" and not modulo.startswith("body."):
        raise TypeError(
            f"texto_propio() called on {type(exc).__name__} from module {modulo!r}, which is "
            "not one of body's own exception classes -- use texto_seguro() instead"
        )
    return str(exc)
