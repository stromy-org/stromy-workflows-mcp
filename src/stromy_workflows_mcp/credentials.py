"""The workflow plane's BYOK credential surface (ORG-PLAN-206 C4).

Three things live here: the closed credential **catalogue**, the process-wide
**grant store**, and the lazily-built Key Vault **store**. Everything else about
BYOK — naming, grants, routes, validators, the scrub-before-inject scope — comes
from the shared `stromy_byok` library, which is client-neutral by construction.

WHY A MIRROR. The catalogue below mirrors the credential declarations Stromy's
runtime owns (C5), rather than importing them, for the same reason
`entitlements.KNOWN_INPUT_ADAPTERS` mirrors Stromy's adapter registry: the
runner lives in a private repo this public one cannot read. The mirror is the
*registration* view — what a client can be asked to hand over — while Stromy
keeps the *resolution* view. `x-credential-requirements` in the generated
contract is the join between them, and C5's `credential_manifest_drift` check is
what catches the two drifting apart at runtime.

WHY EVERY SPEC IS `CALLER_BYOK`. This catalogue exists to describe keys a client
registers and spends on. `CredentialCatalogue.caller_funded_env_aliases()`
derives the client-mode scrub list straight from these declarations, so an
`OPERATOR` spec here would be a credential that client mode leaves live in the
environment — precisely the silent-operator-spend path the plane exists to
close. Operator-funded credentials stay ambient on the runner and are never
declared here.

WHY `env_aliases` MATTERS MORE THAN IT LOOKS. Scrubbing is only as complete as
these tuples. `APIFY_API_TOKEN` and `APIFY_TOKEN` are both real names the Apify
SDK reads; declaring one would leave a live caller-funded key in the environment
of a client-mode run.
"""

from __future__ import annotations

import logging
import os
import threading

from stromy_byok import (
    AzureKeyVaultCredentialStore,
    CredentialCatalogue,
    CredentialId,
    CredentialOwner,
    CredentialSpec,
    InMemoryGrantStore,
    NullCredentialStore,
    ProviderProbe,
    RegistrationGrantStore,
)

logger = logging.getLogger(__name__)

#: Named in the audit trail and baked into every grant, so a grant minted by
#: this service can never be spent against another adopter's registration page.
SERVICE = "stromy-workflows-mcp"

CATALOGUE = CredentialCatalogue(
    [
        CredentialSpec(
            credential_id=CredentialId("openai-api"),
            provider="OpenAI",
            owner=CredentialOwner.CALLER_BYOK,
            env_aliases=("OPENAI_API_KEY",),
            display_name="OpenAI API key",
            signup_url="https://platform.openai.com/api-keys",
            probe=ProviderProbe(
                url="https://api.openai.com/v1/models",
                header="Authorization",
                header_template="Bearer {key}",
            ),
        ),
        CredentialSpec(
            credential_id=CredentialId("deepseek-api"),
            provider="DeepSeek",
            owner=CredentialOwner.CALLER_BYOK,
            env_aliases=("DEEPSEEK_API_KEY",),
            display_name="DeepSeek API key",
            signup_url="https://platform.deepseek.com/api_keys",
            probe=ProviderProbe(
                url="https://api.deepseek.com/models",
                header="Authorization",
                header_template="Bearer {key}",
            ),
        ),
        CredentialSpec(
            credential_id=CredentialId("google-genai"),
            provider="Google",
            owner=CredentialOwner.CALLER_BYOK,
            # All three are real names the Google GenAI SDKs read. See the
            # module docstring: an omission here is a scrub hole, not a typo.
            env_aliases=("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY"),
            display_name="Google AI Studio API key",
            signup_url="https://aistudio.google.com/apikey",
            probe=ProviderProbe(
                url="https://generativelanguage.googleapis.com/v1beta/models",
                header="x-goog-api-key",
                # Gemini answers a bad key with 400, not 401 — the library's
                # validators docstring records the measurement. Declaring 400
                # definitive here is what stops a broken key reading as merely
                # unverified; leaving 401 in covers the shape changing back.
                invalid_statuses=frozenset({400, 401, 403}),
            ),
        ),
        CredentialSpec(
            credential_id=CredentialId("apify-api"),
            provider="Apify",
            owner=CredentialOwner.CALLER_BYOK,
            env_aliases=("APIFY_API_TOKEN", "APIFY_TOKEN"),
            display_name="Apify API token",
            signup_url="https://console.apify.com/settings/integrations",
            probe=ProviderProbe(
                url="https://api.apify.com/v2/users/me",
                header="Authorization",
                header_template="Bearer {key}",
            ),
        ),
        CredentialSpec(
            credential_id=CredentialId("hunter-api"),
            provider="Hunter",
            owner=CredentialOwner.CALLER_BYOK,
            env_aliases=("HUNTER_API_KEY",),
            display_name="Hunter API key",
            signup_url="https://hunter.io/api-keys",
            probe=ProviderProbe(
                url="https://api.hunter.io/v2/account",
                header="X-API-KEY",
            ),
        ),
    ]
)

#: In-memory on purpose. A restart drops pending grants, which is the correct
#: behaviour — mint a fresh link — and keeps a credential-binding capability out
#: of any durable store. `assert_single_replica_or_durable` (readiness) is what
#: refuses this store on a multi-replica deployment rather than leaving the
#: single-replica requirement as documentation.
GRANTS: RegistrationGrantStore = InMemoryGrantStore()

_store_lock = threading.Lock()
_store: object | None = None


def key_vault_url() -> str:
    """The application-scoped vault, delivered as ACA env (AC3)."""
    return os.environ.get("BYOK_KEY_VAULT_URL", "").strip()


def credential_store() -> object:
    """The vault-backed store, or a null reader before one is provisioned.

    Built once and cached: `DefaultAzureCredential` does its own token caching,
    and rebuilding per request would re-do IMDS discovery on every call. Returns
    `NullCredentialStore` rather than raising when the URL is unset, so the
    server starts and reports its state through readiness instead of crashing —
    and `require_writer` still refuses the write path loudly, because that store
    declares `writable = False`.
    """
    global _store
    with _store_lock:
        if _store is None:
            url = key_vault_url()
            if not url:
                logger.warning("BYOK_KEY_VAULT_URL is unset; credential registration is disabled")
                _store = NullCredentialStore()
            else:
                _store = AzureKeyVaultCredentialStore(url, catalogue=CATALOGUE)
        return _store


def reset_store_for_tests() -> None:
    """Drop the cached store so a test can vary `BYOK_KEY_VAULT_URL`."""
    global _store
    with _store_lock:
        _store = None
