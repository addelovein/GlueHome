"""Secrets: environment -> OS credential vault -> encrypted headless storage.

Headless fallback requires GLUE_STORAGE_KEY (base64, 32 random bytes) supplied by
an environment/secret manager. Never generates a plaintext key file. Legacy
plaintext secrets.json and token files are deliberately not read or overwritten.
"""
import base64
import getpass
import json
import os
import sys

from glue_storage import config_dir, atomic_write, locked

SERVICE = 'glue-local'


def _env_name(purpose):
    return 'GLUE_' + purpose.replace('-', '_').upper()


def _keyring():
    # Explicit headless mode is consistent for reads and writes; never mix an
    # old vault value with a newer encrypted-file fallback after a vault error.
    if os.environ.get('GLUE_STORAGE_KEY'):
        return None
    try:
        import keyring
        backend = keyring.get_keyring()
        # Do not accept optional plaintext/file or arbitrary chained backends.
        if type(backend).__module__ not in (
            'keyring.backends.macOS', 'keyring.backends.Windows',
            'keyring.backends.SecretService', 'keyring.backends.kwallet',
        ):
            return None
        return keyring
    except Exception:
        return None


def _cipher():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    value = os.environ.get('GLUE_STORAGE_KEY')
    if not value:
        raise RuntimeError('No usable OS keyring. Supply GLUE_STORAGE_KEY through your secret manager (base64, 32 bytes).')
    key = base64.b64decode(value, validate=True)
    if len(key) != 32:
        raise ValueError('GLUE_STORAGE_KEY must decode to 32 bytes')
    return AESGCM(key)


def _file_read():
    path = config_dir() / 'secrets.enc'
    if path.is_symlink():
        raise RuntimeError('Refusing symlinked secret store')
    if not path.exists():
        return {}
    data = path.read_bytes()
    # Authenticated decryption failure must never be mistaken for an empty store.
    return json.loads(_cipher().decrypt(data[:12], data[12:], SERVICE.encode()))


def get(purpose, default=None):
    # Internal mutable records must never be shadowed by an immutable env value:
    # that would resurrect a key after its reservation was persisted.
    value = None if purpose.startswith(('bank-', 'token-', 'controller-')) else os.environ.get(_env_name(purpose))
    if value:
        return value
    kr = _keyring()
    if kr:
        try:
            value = kr.get_password(SERVICE, purpose)
            if value is not None:
                return value
        except Exception:
            if not os.environ.get('GLUE_STORAGE_KEY'):
                raise RuntimeError('OS keyring is unavailable or locked') from None
    if os.environ.get('GLUE_STORAGE_KEY'):
        with locked('secret-store'):
            return _file_read().get(purpose, default)
    return default


def set_(purpose, value):
    kr = _keyring()
    if kr:
        try:
            kr.set_password(SERVICE, purpose, value)
            return 'keyring'
        except Exception:
            raise RuntimeError('OS keyring write failed; unlock it or explicitly select encrypted storage') from None
    cipher = _cipher()
    with locked('secret-store'):
        data = _file_read()
        data[purpose] = value
        nonce = os.urandom(12)
        atomic_write(config_dir() / 'secrets.enc', nonce + cipher.encrypt(nonce, json.dumps(data).encode(), SERVICE.encode()))
    return 'encrypted-file'


def backend_name():
    kr = _keyring()
    if kr:
        return 'keyring:' + type(kr.get_keyring()).__name__
    return 'encrypted-file (GLUE_STORAGE_KEY required)'


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'where'
    if cmd == 'set' and len(sys.argv) == 3:
        print('stored via', set_(sys.argv[2], getpass.getpass('Value: ')))
    elif cmd == 'where':
        print(backend_name())
    else:
        sys.exit('usage: glue_store.py set <purpose> | where (secret values are prompted)')
