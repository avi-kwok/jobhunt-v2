import pytest

from jobhunt import crypto

URL = "https://discord.com/api/webhooks/123456789012345678/" + "a" * 64


def test_round_trip_and_ciphertext_hides_url():
    enc = crypto.encrypt_url(URL)
    assert URL not in enc and "discord" not in enc
    assert crypto.decrypt_url(enc) == URL


def test_wrong_key_fails():
    enc = crypto.encrypt_url(URL)
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_url(enc, key=crypto.generate_secret_key())


@pytest.mark.parametrize("bad", ["", "not base64!!", "c2hvcnQ"])
def test_missing_or_malformed_key_fails_fast(monkeypatch, bad):
    monkeypatch.setenv("JOBHUNT_SECRET_KEY", bad)
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt_url(URL)
    with pytest.raises(crypto.CryptoError):
        crypto.require_key()
    monkeypatch.delenv("JOBHUNT_SECRET_KEY")
    with pytest.raises(crypto.CryptoError):
        crypto.webhook_hash(URL)


def test_hash_is_stable_keyed_and_domain_separated():
    h = crypto.webhook_hash(URL)
    assert h == crypto.webhook_hash(URL) and len(h) == 64
    assert h != crypto.webhook_hash(URL, key=crypto.generate_secret_key())
    assert h != crypto.webhook_hash(URL + "x")
    assert crypto._derive(None, crypto._INFO_ENC) != crypto._derive(None, crypto._INFO_MAC)


def test_tokens():
    t1, t2 = crypto.new_token(), crypto.new_token()
    assert t1 != t2 and len(t1) >= 43
    assert crypto.token_hash(t1) == crypto.token_hash(t1) != crypto.token_hash(t2)
    assert t1 not in crypto.token_hash(t1)


def test_generated_key_is_valid():
    crypto.require_key(crypto.generate_secret_key())
