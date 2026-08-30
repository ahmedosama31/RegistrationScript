"""Direct HTTP/API entry point for RegistrationBOT (never launches Chrome)."""

from register import run_entrypoint


if __name__ == "__main__":
    run_entrypoint("api")
