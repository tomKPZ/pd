"""Change a side's password and/or the salt (docs/install.md, "Changing a
password").

The boot volume is the only thing keyed by the password: it's read into RAM
with the old key, re-created in place under the new key, written back, and
compared.  data.key, and so the data region, doesn't change.
"""

import os
import pathlib
import stat

from pd import layout as L
from pd import setup
from pd.config import Config
from pd.util import argon2


def snapshot(root: pathlib.Path) -> dict[str, tuple[str, int, bytes]]:
    """Every entry under root: relative path -> (kind, mode, content or link target)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = pathlib.Path(dirpath) / name
            rel = str(path.relative_to(root))
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", 0, os.readlink(path).encode())
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", stat.S_IMODE(st.st_mode), b"")
            else:
                out[rel] = ("file", stat.S_IMODE(st.st_mode), path.read_bytes())
    return out


def restore(root: pathlib.Path, snap: dict[str, tuple[str, int, bytes]]) -> None:
    for rel in sorted(snap, key=lambda r: r.count("/")):
        kind, mode, data = snap[rel]
        path = root / rel
        if kind == "dir":
            path.mkdir(mode=mode)
            os.chmod(path, mode)
        elif kind == "link":
            os.symlink(data.decode(), path)
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.chmod(path, mode)
    os.sync()


def rekey(cfg: Config, offset: int, old_password: bytes, new_password: bytes,
          new_salt: str | None = None) -> None:
    """offset is the boot volume's start in bytes."""
    if old_password == new_password and not new_salt:
        raise RuntimeError("nothing to change")
    old_key = argon2(old_password, cfg.salt, cfg.argon2)
    with setup.boot_volume(cfg.disk, offset, old_key, create=False) as mnt:
        snap = snapshot(mnt)
    if "data.key" not in snap:
        raise RuntimeError("that boot volume has no data.key; not a pd boot volume?")
    new_key = argon2(new_password, new_salt or cfg.salt, cfg.argon2)
    if new_key == old_key:
        raise RuntimeError("the new password and salt derive the same key")

    # From here until the comparison below, the only complete copy of this
    # side's boot volume (including data.key) is `snap`, in RAM.
    with setup.boot_volume(cfg.disk, offset, new_key, create=True) as mnt:
        restore(mnt, snap)
    with setup.boot_volume(cfg.disk, offset, new_key, create=False) as mnt:
        if snapshot(mnt) != snap:
            raise RuntimeError("verification failed: the rewritten boot volume differs")


def default_offset(cfg: Config, sectors: int | None) -> int:
    return L.DECOY_PLAIN_OFFSET if sectors is None else sectors * L.SECTOR
