# BIP-340 test vectors

`test-vectors.csv` is the official test vector file of BIP-340 (Schnorr signatures for
secp256k1), unchanged:

- source: <https://github.com/bitcoin/bips/blob/927b6de9915c9262615a6399de51b200f81e5aa4/bip-0340/test-vectors.csv>
  (bitcoin/bips, commit `927b6de9915c9262615a6399de51b200f81e5aa4`);
- SHA-256: `34c9d1d9c3a88d524bc80778540dc43f8306ec249a7485293063c376db851c2d`, checked by
  `tests/test_bip340.py`;
- license: BIP-340 offers its test vectors "under the BSD-2-Clause License, or the MIT License,
  or CC0 1.0, at your choice"; this repository takes them under the MIT License.

They are not part of checkout-spec: they check the Schnorr signatures of the secp256k1 library
this package uses (`btclib-secp256k1`, bindings to libsecp256k1), with 32 bytes of auxiliary
randomness.
