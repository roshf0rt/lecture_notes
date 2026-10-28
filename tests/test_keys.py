import os
import stat

from notes_bot.keys import KeyVault

KEY = "AIzaSyTestKeyValue1234567890abcdefghij"


def test_save_load_and_delete(tmp_path):
    vault = KeyVault(tmp_path)
    assert vault.load(1) is None

    vault.save(1, KEY)
    assert vault.load(1) == KEY

    vault.save(1, KEY + "x")
    assert vault.load(1) == KEY + "x"

    vault.delete(1)
    assert vault.load(1) is None
    vault.close()


def test_key_is_not_stored_in_plain_text(tmp_path):
    vault = KeyVault(tmp_path)
    vault.save(1, KEY)
    vault.close()
    assert KEY.encode() not in (tmp_path / "keys.sqlite3").read_bytes()


def test_keys_survive_a_restart_with_the_same_secret(tmp_path):
    first = KeyVault(tmp_path)
    first.save(7, KEY)
    first.close()

    second = KeyVault(tmp_path)
    assert second.load(7) == KEY
    second.close()


def test_secret_file_is_private(tmp_path):
    KeyVault(tmp_path).close()
    mode = stat.S_IMODE(os.stat(tmp_path / "secret.key").st_mode)
    assert mode == 0o600


def test_key_cannot_be_read_with_a_different_secret(tmp_path):
    vault = KeyVault(tmp_path)
    vault.save(1, KEY)
    vault.close()
    (tmp_path / "secret.key").unlink()

    other = KeyVault(tmp_path)
    assert other.load(1) is None
    other.close()
