"""TLS verification for public-data clients on Windows hosts."""

from __future__ import annotations

import os
import ssl
import sys


def httpx_verify() -> ssl.SSLContext | bool:
    """Use Windows' trusted certificates without weakening TLS verification.

    HTTPX otherwise uses certifi, which excludes locally trusted certificates
    such as an antivirus HTTPS scanner's root. Explicit CA overrides and other
    platforms retain HTTPX's normal trust configuration and error handling.
    """
    if (
        sys.platform == "win32"
        and not os.environ.get("SSL_CERT_FILE")
        and not os.environ.get("SSL_CERT_DIR")
    ):
        return ssl.create_default_context()
    return True
