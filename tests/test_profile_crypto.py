"""
Field-level encryption used for candidate profile PII (phone, address).
No DB required — pure encrypt/decrypt round trip against app/core/crypto.py.
"""

from app.core.crypto import decrypt_field, encrypt_field


def test_round_trip_encrypts_and_decrypts():
    original = "+91 98765 43210"
    ciphertext = encrypt_field(original)

    assert ciphertext is not None
    assert ciphertext != original  # never stored in plaintext
    assert decrypt_field(ciphertext) == original


def test_none_passes_through_unchanged():
    assert encrypt_field(None) is None
    assert decrypt_field(None) is None


def test_corrupt_ciphertext_decrypts_to_none_not_a_crash():
    assert decrypt_field("not-a-real-fernet-token") is None


def test_two_encryptions_of_same_value_are_not_identical():
    # Fernet includes a random IV/timestamp, so ciphertexts differ even for
    # the same plaintext — this is expected, not a bug.
    a = encrypt_field("secret")
    b = encrypt_field("secret")
    assert a != b
    assert decrypt_field(a) == decrypt_field(b) == "secret"
