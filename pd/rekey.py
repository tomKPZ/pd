"""Change a side's password and/or the salt (docs/install.md, "Changing a
password"), and enroll FIDO2 security keys (design.md §4.1).

The boot volume is the only thing keyed by the password: it's read into RAM
with the old key, re-created in place under the new key, written back, and
compared.  data.key, and so the data region, doesn't change.
"""

import os
import pathlib
import stat

from pd import fido2, header
from pd import layout as L
from pd import setup
from pd.config import Config
from pd.util import Argon2Params, argon2


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


def rewrite_boot_volume(cfg: Config, offset: int, old_key: bytes, new_key: bytes, edit=None) -> None:
    """Re-create the boot volume at offset under new_key, with the same
    contents (or `edit(snapshot)`'s).  The whole area is refilled with random
    data first, so nothing of the old volume survives in the new one's free
    space (e.g. a plaintext data.key when FIDO2 starts protecting it)."""
    with setup.boot_volume(cfg.disk, offset, old_key, create=False) as mnt:
        snap = snapshot(mnt)
    if "data.key" not in snap and fido2.INNER not in snap:
        raise RuntimeError("that boot volume has no data.key; not a pd boot volume?")
    if edit:
        snap = edit(snap)
    # From here until the comparison below, the only complete copy of this
    # side's boot volume (including its keys) is `snap`, in RAM.
    setup.wipe_boot_volume(cfg.disk, offset)
    with setup.boot_volume(cfg.disk, offset, new_key, create=True) as mnt:
        restore(mnt, snap)
    with setup.boot_volume(cfg.disk, offset, new_key, create=False) as mnt:
        if snapshot(mnt) != snap:
            raise RuntimeError("verification failed: the rewritten boot volume differs")


def no_more_keys() -> bool:
    return False


def rekey(cfg: Config, offset: int, old_password: bytes, new_password: bytes,
          new_salt: str | None = None, next_key=no_more_keys,
          new_argon2: Argon2Params | None = None) -> list[str]:
    """offset is the boot volume's start in bytes.  Returns warnings.

    A side without FIDO2 gets its boot volume re-created under the new
    Argon2 output.  A side with FIDO2 gets a new random boot volume key too,
    wrapped under the new password for each security key, one at a time:
    next_key() asks for the next one to be plugged in and returns False
    when there are no more.  (Keeping the old key would let an old copy of
    fido2.txt, the old password and a security key still open the side.)
    The slots of keys that weren't plugged in are replaced with random
    bytes.  The new slots go to fido2.txt.new first and replace fido2.txt
    once the boot volume is re-keyed; if that's interrupted, the boot
    volume opens with fido2.txt.new (or, before the rewrite, fido2.txt)."""
    if old_password == new_password and not new_salt and not new_argon2:
        raise RuntimeError("nothing to change")
    found = setup.find_boot_key(cfg, offset, old_password)
    if found is None:
        raise RuntimeError("the current password doesn't open a boot volume at that offset")
    old_key, slot = found
    new_a = argon2(new_password, new_salt or header.salt(cfg), new_argon2 or cfg.argon2)
    if slot is None:
        if new_a == old_key:
            raise RuntimeError("the new password and salt derive the same key")
        if uses_data_mode(cfg, offset, old_key):
            return rekey_data(cfg, offset, old_key, new_a, next_key)
        rewrite_boot_volume(cfg, offset, old_key, new_a)
        return []
    creds = fido2.load(cfg)
    new_key = os.urandom(fido2.KEY_LEN)
    done: set[int] = set()
    while True:
        for dev, i in fido2.holders(creds).items():
            if i not in done:
                creds[i].slots[slot] = fido2.wrap_for(new_a, new_key, dev, creds[i].id)
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
    if not done:
        raise RuntimeError("no enrolled security key was plugged in; nothing changed")
    if not cfg.fido2:
        raise RuntimeError("set `fido2 = \"fido2.txt\"` in the config")
    path = pathlib.Path(cfg.fido2)
    staged = path.with_name(path.name + ".new")
    staged.write_text(fido2.dump(creds))
    rewrite_boot_volume(cfg, offset, old_key, new_key)
    os.replace(staged, path)
    cfg.fido2_text = None
    return warnings


def uses_data_mode(cfg: Config, offset: int, key: bytes) -> bool:
    """Does the boot volume at offset (opened by key) hold FIDO2 data-mode
    slots?"""
    with setup.boot_volume(cfg.disk, offset, key, create=False) as mnt:
        return (mnt / fido2.INNER).exists()


def _replace_inner(creds: list) -> callable:
    def edit(snap: dict) -> dict:
        snap = {k: v for k, v in snap.items() if k not in ("data.key", "decoy.key")}
        snap[fido2.INNER] = ("file", 0o600, fido2.dump(creds).encode())
        return snap
    return edit


def _wrap_for_keys(creds: list, a: bytes, secrets: list[bytes], next_key, *, add: bool) -> tuple[set, int]:
    """Wrap `secrets` under A for each security key the user plugs in, one at
    a time (next_key() asks for the next).  With `add`, a key without a
    credential gets one.  Returns (indices done, credentials created)."""
    done: set[int] = set()
    created = 0
    while True:
        held = fido2.holders(creds)
        if add:
            for dev in fido2.devices():
                if dev not in held:
                    creds.append(fido2.Credential(fido2.make(dev), [b""] * fido2.SLOTS))
                    held[dev] = len(creds) - 1
                    created += 1
        for dev, i in held.items():
            if i not in done:
                creds[i].slots = fido2.inner_slots(a, dev, creds[i].id, secrets)
                done.add(i)
        if not next_key():
            break
        fido2.forget_pins()
    return done, created


def enroll_data(cfg: Config, offset: int, password: bytes, next_key=no_more_keys) -> tuple[int, int]:
    """Data mode (pd/fido2.py): the boot volume stays keyed by A, and its
    data.key (and, on the hidden side, decoy.key) are replaced by slots for
    each security key in fido2.txt inside it.  A side already in data mode
    keeps its credentials; the first plugged-in key must be an enrolled one.
    A side in boot mode is converted."""
    if not fido2.devices():
        raise RuntimeError("no FIDO2 security key found")
    found = setup.find_boot_key(cfg, offset, password)
    if found is None:
        raise RuntimeError("that password doesn't open a boot volume at that offset")
    key, slot = found
    a = argon2(password, header.salt(cfg), cfg.argon2)
    with setup.boot_volume(cfg.disk, offset, key, create=False) as mnt:
        files = {n: (mnt / n).read_bytes() for n in setup.SECRET_FILES if (mnt / n).exists()}
    data_key, decoy_key = fido2.volume_secrets(files, key)
    # Slot 1 is the decoy's data key on the hidden side, random on the decoy.
    secrets = [data_key, decoy_key if decoy_key is not None else os.urandom(fido2.KEY_LEN)]
    creds = fido2.parse(files[fido2.INNER].decode()) if fido2.INNER in files else []
    done, created = _wrap_for_keys(creds, a, secrets, next_key, add=True)
    rewrite_boot_volume(cfg, offset, key, a, _replace_inner(creds))
    if slot is not None:
        # Converted from boot mode: its slot on the USB no longer opens anything.
        usb = fido2.load(cfg)
        for c in usb:
            c.slots[slot] = os.urandom(fido2.KEY_LEN)
        if usb:
            fido2.save(cfg, usb)
    return len(done), created


def rekey_data(cfg: Config, offset: int, old_key: bytes, new_a: bytes, next_key) -> list[str]:
    """A new password (or salt) for a data-mode side: the slots are wrapped
    again under the new A, one security key at a time; keys that weren't
    plugged in are dropped."""
    with setup.boot_volume(cfg.disk, offset, old_key, create=False) as mnt:
        text = (mnt / fido2.INNER).read_text()
    creds = fido2.parse(text)
    secrets = fido2.inner_unwrap(text, old_key)
    done, _ = _wrap_for_keys(creds, new_a, secrets, next_key, add=False)
    if not done:
        raise RuntimeError("no enrolled security key was plugged in; nothing changed")
    warnings = [f"security key {i} wasn't plugged in; it no longer opens this side "
                "(`pd fido2 enroll` to add it back)" for i in range(len(creds)) if i not in done]
    keep = [c for i, c in enumerate(creds) if i in done]
    rewrite_boot_volume(cfg, offset, old_key, new_a, _replace_inner(keep))
    return warnings


def update_decoy_copy(cfg: Config, offset: int, password: bytes, decoy_key: bytes, decoy_table: str,
                      next_key=no_more_keys) -> list[str]:
    """After the decoy is re-created: replace the hidden side's copies of its
    data key and table (the frontier monitor's).  In data mode the decoy key
    is a slot, so each security key has to be plugged in again; the ones that
    aren't stop working for the hidden side."""
    found = setup.find_boot_key(cfg, offset, password)
    if found is None:
        raise RuntimeError("the hidden password doesn't open the hidden boot volume")
    key, _ = found
    with setup.boot_volume(cfg.disk, offset, key, create=False) as mnt:
        files = {n: (mnt / n).read_bytes() for n in setup.SECRET_FILES if (mnt / n).exists()}
    warnings: list[str] = []
    if fido2.INNER in files:
        data_key, _ = fido2.volume_secrets(files, key)
        creds = fido2.parse(files[fido2.INNER].decode())
        done, _ = _wrap_for_keys(creds, key, [data_key, decoy_key], next_key, add=False)
        if not done:
            raise RuntimeError("no enrolled security key was plugged in for the hidden side")
        warnings = [f"security key {i} wasn't plugged in; it no longer opens the hidden side"
                    for i in range(len(creds)) if i not in done]
        replace = _replace_inner([c for i, c in enumerate(creds) if i in done])
    else:
        def replace(snap: dict) -> dict:
            return dict(snap, **{"decoy.key": ("file", 0o600, decoy_key)})

    def edit(snap: dict) -> dict:
        snap = replace(snap)
        snap["decoy.table"] = ("file", snap["decoy.table"][1], decoy_table.encode())
        return snap
    rewrite_boot_volume(cfg, offset, key, key, edit)
    return warnings


def data_creds(cfg: Config, offset: int, password: bytes) -> tuple[bytes, list]:
    key = setup.boot_key(cfg, offset, password)
    with setup.boot_volume(cfg.disk, offset, key, create=False) as mnt:
        if not (mnt / fido2.INNER).exists():
            raise RuntimeError("this side has no security keys enrolled in data mode")
        return key, fido2.parse((mnt / fido2.INNER).read_text())


def remove_data(cfg: Config, offset: int, password: bytes, indices: list[int]) -> None:
    key, creds = data_creds(cfg, offset, password)
    if any(i < 0 or i >= len(creds) for i in indices):
        raise RuntimeError(f"no such security key (there are {len(creds)})")
    keep = [c for i, c in enumerate(creds) if i not in indices]
    if not keep:
        raise RuntimeError("refusing to remove every security key: the side would be lost")
    rewrite_boot_volume(cfg, offset, key, key, _replace_inner(keep))


def status_data(cfg: Config, offset: int, password: bytes) -> str:
    _, creds = data_creds(cfg, offset, password)
    held = {i: dev for dev, i in fido2.holders(creds).items()}
    return "\n".join(f"{i}: {c.id.hex()[:16]}…  {'plugged in: ' + held[i] if i in held else ''}".rstrip()
                     for i, c in enumerate(creds))


def enroll(cfg: Config, offset: int, password: bytes, next_key=no_more_keys) -> tuple[int, int]:
    """Enroll security keys for the side at offset, one at a time: the
    plugged-in one first, then each one next_key() asks for, until it
    returns False.  Returns (security keys enrolled, new credentials).

    If the side already uses FIDO2, the first plugged-in key must be an
    enrolled one (it unlocks the side).  Otherwise the side gets a random
    boot volume key, and after that the password alone no longer opens it.
    A key without a credential gets one (a new fido2.txt line, whose slot
    for the other side stays random until that side enrolls it)."""
    if cfg.fido2_mode == "data":
        return enroll_data(cfg, offset, password, next_key)
    if not fido2.devices():
        raise RuntimeError("no FIDO2 security key found")
    found = setup.find_boot_key(cfg, offset, password)
    if found is None:
        raise RuntimeError("that password doesn't open a boot volume at that offset"
                           + (" (with the plugged-in security key)" if fido2.load(cfg) else ""))
    key, slot = found
    if slot is None and uses_data_mode(cfg, offset, key):
        raise RuntimeError("this side's security keys are enrolled in data mode (fido2_mode)")
    a = argon2(password, header.salt(cfg), cfg.argon2)
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
