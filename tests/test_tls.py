import ssl
from types import SimpleNamespace

import httpx
import pytest

from liquidity_signal.data import tls


@pytest.fixture
def windows_trust(monkeypatch):
    monkeypatch.setattr(tls, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


def test_windows_context_requires_certificates_and_hostname(windows_trust):
    context = tls.httpx_verify()

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.cert_store_stats()["x509_ca"] > 0


@pytest.mark.parametrize("name", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_explicit_ca_override_keeps_httpx_configuration(windows_trust, monkeypatch, name):
    monkeypatch.setenv(name, "explicit-ca-location")

    assert tls.httpx_verify() is True


def test_missing_ca_file_fails_instead_of_falling_back_to_windows(
    windows_trust, monkeypatch, tmp_path
):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))

    with pytest.raises(FileNotFoundError):
        httpx.Client(verify=tls.httpx_verify())


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platforms_keep_httpx_default_trust(windows_trust, monkeypatch, platform):
    monkeypatch.setattr(tls, "sys", SimpleNamespace(platform=platform))

    assert tls.httpx_verify() is True
