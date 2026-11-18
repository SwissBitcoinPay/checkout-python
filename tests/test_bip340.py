"""BIP-340 signatures of the chosen secp256k1 library, against BIP-340's official vectors."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import pytest
from btclib_secp256k1 import ssa

from sattleme_checkout._crypto import Secret, schnorr_sign, schnorr_verify, xonly_public_key

CSV = Path(__file__).resolve().parent / "vectors" / "bip340" / "test-vectors.csv"


def _rows() -> list[dict[str, str]]:
    with CSV.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def test_the_file_is_the_official_one() -> None:
    digest = hashlib.sha256(CSV.read_bytes()).hexdigest()
    assert digest == "34c9d1d9c3a88d524bc80778540dc43f8306ec249a7485293063c376db851c2d"
    assert len(_rows()) == 19


@pytest.mark.parametrize("row", _rows(), ids=lambda row: row["index"])
def test_vector(row: dict[str, str]) -> None:
    public_key = bytes.fromhex(row["public key"])
    message = bytes.fromhex(row["message"])
    signature = bytes.fromhex(row["signature"])
    expected = row["verification result"] == "TRUE"
    if row["secret key"]:
        secret = Secret(bytes.fromhex(row["secret key"]))
        aux = bytes.fromhex(row["aux_rand"])
        assert xonly_public_key(secret) == public_key
        if len(message) == 32:
            assert schnorr_sign(message, secret, aux) == signature
        else:
            # Nostr signs 32-byte ids only; the library's other entry point covers the rest.
            assert ssa.sign_custom(message, secret.reveal(), aux) == signature
    if len(message) == 32:
        assert schnorr_verify(message, public_key, signature) is expected
    else:
        try:
            verified = bool(ssa.verify(message, public_key, signature))
        except ValueError:
            verified = False
        assert verified is expected
