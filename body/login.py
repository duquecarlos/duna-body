"""First sign-in to Garmin Connect. Carlos runs this himself, in his own terminal.

The password is typed into getpass (not echoed, not saved, not logged). Only the token
that Garmin returns is kept, in BODY_HOME/garmin_tokens, outside the repo.
"""
import getpass
import sys

from . import config, errores, garmin_adapter


def main() -> int:
    print("Body app - Garmin sign-in")
    print(f"The token will be saved in: {config.TOKEN_DIR}")
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (hidden): ")
    try:
        garmin_adapter.sign_in(email, password, prompt_mfa=lambda: input("Garmin code (MFA): ").strip())
    except Exception as e:
        print(f"Sign-in failed: {errores.texto_seguro(e)}")
        return 1
    finally:
        password = None  # noqa: F841
    print("Signed in. Token saved. Next: python -m body.pull")
    return 0


if __name__ == "__main__":
    sys.exit(main())
