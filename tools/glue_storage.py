"""Private atomic files and process locks; callers must encrypt secret contents."""
import contextlib
import hashlib
import os
from pathlib import Path
import tempfile


def config_dir():
    path = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'glue-local'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError('Refusing symlinked Glue storage directory')
    if os.name != 'nt':
        path.chmod(0o700)
    return path


def atomic_write(path, data):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('Refusing symlinked storage file')
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name != 'nt':
            d = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(d)
            finally:
                os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(name):
    path = config_dir() / (hashlib.sha256(name.encode()).hexdigest() + '.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+b') as f:
        if os.name == 'nt':
            import msvcrt
            if not f.read(1):
                f.write(b'0'); f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
