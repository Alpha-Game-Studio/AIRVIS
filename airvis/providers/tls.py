"""TLS configuration for outbound provider HTTP.

macOS Python installations can lack the system CA bundle even though HTTPS
works normally in browsers. AIRVIS must never disable certificate verification;
when available, certifi supplies a portable CA bundle instead.
"""

from __future__ import annotations

import ssl
import urllib.request


def install_secure_default_opener() -> None:
    """Install a verified HTTPS opener using certifi when available."""
    context = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        cert_path = ""
    else:
        cert_path = certifi.where()

    if cert_path:
        context.load_verify_locations(cafile=cert_path)

    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    urllib.request.install_opener(opener)


__all__ = ["install_secure_default_opener"]
