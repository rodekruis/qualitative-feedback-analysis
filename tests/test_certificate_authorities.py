"""Certificate authority trust-store checks."""

import ssl

import certifi


def test_certifi_excludes_compromised_emsign_root() -> None:
    context = ssl.create_default_context(cafile=certifi.where())
    common_names = {
        value
        for certificate in context.get_ca_certs()
        for relative_name in certificate["subject"]
        for key, value in relative_name
        if key == "commonName"
    }

    assert "emSign ECC Root CA - G3" not in common_names
