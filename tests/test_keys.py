import os
import sqlite3
import stat

from cryptography.fernet import Fernet

from notes_bot.keys import KEY_PATTERN, MAX_USER_KEYS, POOL, KeyVault, mask

KEY = "AIzaSyTestKeyValue1234567890abcdefghij"
OTHER = "AQ.TestKeyValue1234567890abcdefghijklmnop"


def test_a_user_keeps_several_keys_in_order(tmp_path):
    vault = KeyVault(tmp_path)
    assert vault.values(1) == []
    assert vault.add(1, KEY)
    assert vault.add(1, OTHER)
    assert vault.values(1) == [KEY, OTHER]
    assert vault.count(1) == 2

    first = vault.list(1)[0]
    vault.remove(1, first.id)
    assert vault.values(1) == [OTHER]
    vault.clear(1)
    assert vault.values(1) == []
    vault.close()


def test_duplicates_and_overflow_are_refused(tmp_path):
    vault = KeyVault(tmp_path)
    assert vault.add(1, KEY)
    assert not vault.add(1, KEY)
    for number in range(MAX_USER_KEYS - 1):
        assert vault.add(1, f"{KEY}{number}")
    assert not vault.add(1, KEY + "overflow")
    vault.close()


def test_users_and_the_shared_pool_are_separate(tmp_path):
    vault = KeyVault(tmp_path)
    vault.add(1, KEY)
    vault.add(POOL, OTHER)
    vault.remove(1, vault.list(POOL)[0].id)  # another owner's id does nothing
    assert vault.values(POOL) == [OTHER]
    assert vault.values(1) == [KEY]
    assert vault.owners_with_keys() == 1
    vault.close()


def test_keys_are_not_stored_in_plain_text(tmp_path):
    vault = KeyVault(tmp_path)
    vault.add(1, KEY)
    vault.close()
    assert KEY.encode() not in (tmp_path / "keys.sqlite3").read_bytes()


def test_keys_survive_a_restart_with_the_same_secret(tmp_path):
    KeyVault(tmp_path).add(7, KEY)
    assert KeyVault(tmp_path).values(7) == [KEY]


def test_secret_file_is_private(tmp_path):
    KeyVault(tmp_path).close()
    mode = stat.S_IMODE(os.stat(tmp_path / "secret.key").st_mode)
    assert mode == 0o600


def test_keys_cannot_be_read_with_a_different_secret(tmp_path):
    KeyVault(tmp_path).add(1, KEY)
    (tmp_path / "secret.key").unlink()
    assert KeyVault(tmp_path).values(1) == []


def test_single_keys_from_version_one_are_migrated(tmp_path):
    secret = Fernet.generate_key()
    (tmp_path / "secret.key").write_bytes(secret)
    connection = sqlite3.connect(tmp_path / "keys.sqlite3")
    connection.execute("CREATE TABLE api_keys (user_id INTEGER PRIMARY KEY, token BLOB NOT NULL)")
    connection.execute("INSERT INTO api_keys VALUES (?, ?)", (5, Fernet(secret).encrypt(KEY.encode())))
    connection.commit()
    connection.close()

    vault = KeyVault(tmp_path)
    assert vault.values(5) == [KEY]
    vault.close()
    assert KeyVault(tmp_path).values(5) == [KEY]  # migrated once, not twice


def test_key_pattern_and_mask():
    assert KEY_PATTERN.findall(f"вот {KEY}\n{OTHER}") == [KEY, OTHER]
    assert mask(KEY) == "AIza…ghij"
