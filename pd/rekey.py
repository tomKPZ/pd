"""Change a side's password and/or the salt (docs/install.md, "Changing a
password"), and enroll FIDO2 security keys (design.md §4.1).

The boot volume is the only thing keyed by the password: it's read into RAM
with the old key, re-created in place under the new key, written back, and
compared.  data.key, and so the data region, doesn't change.
"""

import os
import pathlib
import stat

from pd import fido2
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


def rewrite_boot_volume(cfg: Config, offset: int, old_key: bytes, new_key: bytes) -> None:
    """Re-create the boot volume at offset under new_key, same contents."""
    with setup.boot_volume(cfg.disk, offset, old_key, create=False) as mnt:
        snap = snapshot(mnt)
    if "data.key" not in snap:
        raise RuntimeError("that boot volume has no data.key; not a pd boot volume?")
    # From here until the comparison below, the only complete copy of this
    # side's boot volume (including data.key) is `snap`, in RAM.
    with setup.boot_volume(cfg.disk, offset, new_key, create=True) as mnt:
        restore(mnt, snap)
    with setup.boot_volume(cfg.disk, offset, new_key, create=False) as mnt:
        if snapshot(mnt) != snap:
            raise RuntimeError("verification failed: the rewritten boot volume differs")


def no_more_keys() -> bool:
    return False


def rekey(cfg: Config, offset: int, old_password: bytes, new_password: bytes,
          new_salt: str | None = None, next_key=no_more_keys) -> list[str]:
    """offset is the boot volume's start in bytes.  Returns warnings.

    A side without FIDO2 gets its boot volume re-created under the new
    Argon2 output.  A side with FIDO2 keeps its (random) boot volume key;
    only its fido2.txt slots are re-wrapped under the new password, one
    security key at a time: next_key() asks for the next one to be plugged
    in and returns False when there are no more.  The slots of keys that
    weren't plugged in are replaced with random bytes, since they would
    still open with the old password."""
    if old_password == new_password and not new_salt:
        raise RuntimeError("nothing to change")
    found = setup.find_boot_key(cfg, offset, old_password)
    if found is None:
        raise RuntimeError("the current password doesn't open a boot volume at that offset")
    old_key, slot = found
    new_a = argon2(new_password, new_salt or cfg.salt, cfg.argon2)
    if slot is None:
        if new_a == old_key:
            raise RuntimeError("the new password and salt derive the same key")
        rewrite_boot_volume(cfg, offset, old_key, new_a)
        return []
    creds = fido2.load(cfg)
    done: set[int] = set()
    while True:
        for dev, i in fido2.holders(creds).items():
            if i not in done:
                creds[i].slots[slot] = fido2.wrap_for(new_a, old_key, dev, creds[i].id)
                done.add(i)
        if len(done) == len(creds) or not next_key():
            break
        fido2.forget_pins()
    warnings = []
    for i, c in enumerate(creds):
        if i not in done:
            c.slots[slot] = os.urandom(fido2.KEY_LEN)
            warnings.append(f"security key {i} wasn't plugged in; it no longer opens this side "
                            "(`pd fido2 enroll` to add it back)")
    fido2.save(cfg, creds)
    return warnings


def enroll(cfg: Config, offset: int, password: bytes, next_key=no_more_keys) -> tuple[int, int]:
    """Enroll security keys for the side at offset, one at a time: the
    plugged-in one first, then each one next_key() asks for, until it
    returns False.  Returns (security keys enrolled, new credentials).

    If the side already uses FIDO2, the first plugged-in key must be an
    enrolled one (it unlocks the side).  Otherwise the side gets a random
    boot volume key, and after that the password alone no longer opens it.
    A key without a credential gets one (a new fido2.txt line, whose slot
    for the other side stays random until that side enrolls it)."""
    if not fido2.devices():
        raise RuntimeError("no FIDO2 security key found")
    found = setup.find_boot_key(cfg, offset, password)
    if found is None:
        raise RuntimeError("that password doesn't open a boot volume at that offset"
                           + (" (with the plugged-in security key)" if fido2.load(cfg) else ""))
    key, slot = found
    a = argon2(password, cfg.salt, cfg.argon2)
    side = fido2.side_slot(offset) if slot is None else slot
    new_key = key if slot is not None else os.urandom(fido2.KEY_LEN)
    creds = fido2.load(cfg)
    done: set[int] = set()
    created = 0
    while True:
        held = fido2.holders(creds)
        for dev in fido2.devices():
            if dev not in held:
                creds.append(fido2.Credential(fido2.make(dev), [os.urandom(fido2.KEY_LEN)
                                                                for _ in range(fido2.SLOTS)]))
                held[dev] = len(creds) - 1
                created += 1
        for dev, i in held.items():
            if i not in done:
                creds[i].slots[side] = fido2.wrap_for(a, new_key, dev, creds[i].id)
                done.add(i)
        if not next_key():
            break
        fido2.forget_pins()
    # Save first: if the rewrite below is interrupted, the boot volume still
    # opens with the password alone (the boot USB tries that too).
    fido2.save(cfg, creds)
    if new_key != key:
        rewrite_boot_volume(cfg, offset, key, new_key)
    return len(done), created


def remove(cfg: Config, indices: list[int]) -> None:
    creds = fido2.load(cfg)
    if any(i < 0 or i >= len(creds) for i in indices):
        raise RuntimeError(f"no such security key (there are {len(creds)})")
    keep = [c for i, c in enumerate(creds) if i not in indices]
    if not keep:
        raise RuntimeError("refusing to remove every security key: the sides would be lost")
    fido2.save(cfg, keep)


def status(cfg: Config) -> str:
    creds = fido2.load(cfg)
    held = {i: dev for dev, i in fido2.holders(creds).items()} if creds else {}
    lines = [f"{i}: {c.id.hex()[:16]}…  {'plugged in: ' + held[i] if i in held else ''}".rstrip()
             for i, c in enumerate(creds)]
    return "\n".join(lines) if lines else "No security keys enrolled."


def default_offset(cfg: Config, sectors: int | None) -> int:
    return L.DECOY_PLAIN_OFFSET if sectors is None else sectors * L.SECTOR
