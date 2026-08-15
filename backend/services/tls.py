"""One verified TLS trust store shared by every outbound HTTPS call.

`ssl.create_default_context()` trusts whatever CA bundle OpenSSL happens to
find. On two common deployments it finds nothing at all: python.org macOS
framework builds ship without a bundle until `Install Certificates.command` is
run, and slim container images frequently omit `ca-certificates`. Verification
then fails for *every* host with CERTIFICATE_VERIFY_FAILED.

Each caller here treats a provider failure as non-fatal and falls back to an
approximate answer, so an empty trust store never surfaced as an error — it
silently degraded the product instead. The Singapore road legs of a
cross-border route were the visible symptom: OSRM was unreachable, so a
journey that should follow real roads was drawn as a smooth five-point
connector with no turns.

Verification is never disabled here. When the system store is empty we load
certifi's bundle instead; if that is unavailable too, the caller's existing
failure path still applies.
"""

import ssl
from functools import lru_cache
from typing import Optional


def _has_trusted_roots(context: ssl.SSLContext) -> bool:
    try:
        return int(context.cert_store_stats().get("x509_ca", 0)) > 0
    except (AttributeError, OSError):
        # Non-CPython or a store that refuses inspection: assume the platform
        # knows better than we do rather than overriding a working default.
        return True


@lru_cache(maxsize=1)
def _certifi_bundle() -> Optional[str]:
    try:
        import certifi
    except ImportError:
        return None
    try:
        return certifi.where()
    except Exception:  # noqa: BLE001 - a broken bundle must not break routing
        return None


def new_context() -> ssl.SSLContext:
    """Build an independent verifying context with CA roots actually loaded.

    Use this when the caller needs to set connection options of its own; the
    shared :func:`default_context` is process-wide, so mutating it would apply
    one host's compatibility workaround to every outbound request.
    """
    context = ssl.create_default_context()
    if _has_trusted_roots(context):
        return context

    bundle = _certifi_bundle()
    if bundle is None:
        print(
            "[tls] No CA certificates are available: the system trust store is "
            "empty and certifi is not installed. Outbound HTTPS will fail and "
            "callers will fall back to offline estimates. Install "
            "backend/requirements.txt, or run Install Certificates.command on "
            "a python.org macOS build."
        )
        return context

    trusted = ssl.create_default_context(cafile=bundle)
    if not _has_trusted_roots(trusted):
        return context
    return trusted


@lru_cache(maxsize=1)
def default_context() -> ssl.SSLContext:
    """Return the shared verifying TLS context. Never mutate the result."""
    return new_context()


def trust_store_status() -> dict:
    """Describe the active trust store for health checks and diagnostics."""
    context = default_context()
    try:
        ca_count = int(context.cert_store_stats().get("x509_ca", 0))
    except (AttributeError, OSError):
        ca_count = -1
    return {
        "verification_enabled": context.verify_mode == ssl.CERT_REQUIRED,
        "check_hostname": context.check_hostname,
        "ca_certificate_count": ca_count,
        "certifi_available": _certifi_bundle() is not None,
    }
