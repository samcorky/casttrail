from casttrail.auth.passwords import DUMMY_HASH, hash_password, verify_password
from casttrail.auth.service import normalise_email
from casttrail.auth.tokens import hash_token, new_token


def test_a_hashed_password_verifies_and_is_not_stored_in_plain_text() -> None:
    hashed = hash_password("correct horse")

    assert "correct horse" not in hashed
    assert hashed.startswith("$argon2id$")
    assert verify_password("correct horse", hashed)


def test_a_wrong_password_does_not_verify() -> None:
    assert not verify_password("wrong", hash_password("correct horse"))


def test_the_same_password_hashes_differently_each_time() -> None:
    assert hash_password("same") != hash_password("same")


def test_the_dummy_hash_is_a_valid_hash_that_matches_no_real_password() -> None:
    assert not verify_password("anything", DUMMY_HASH)


def test_tokens_are_unique_and_long_enough_to_be_unguessable() -> None:
    tokens = {new_token() for _ in range(100)}

    assert len(tokens) == 100
    assert all(len(token) >= 43 for token in tokens)


def test_a_token_hash_is_stable_and_does_not_reveal_the_token() -> None:
    token = new_token()

    assert hash_token(token) == hash_token(token)
    assert token not in hash_token(token)
    assert len(hash_token(token)) == 64


def test_emails_are_trimmed_and_lower_cased() -> None:
    assert normalise_email("  Sam@Example.COM ") == "sam@example.com"
