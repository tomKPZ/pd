"""The boot USB: the release boot image plus optional per-install files.

The boot image (`BOOTX64.EFI`, built by Nix: nix/default.nix bootUki) is the
same file for every install (docs/design.md §7.1).  What differs between
installs goes next to it, in `EFI/BOOT/BOOTX64.EFI.extra.d/`, as `*.cred`
files that systemd-stub packs into a cpio under `/.extra/credentials/` in
the initrd:
  keymap.cred   the console keymap (busybox loadkmap format)
  argon2.cred   Argon2 parameters other than the defaults
  salt.cred     a salt from the config (stock boot; installs from before the
                salt moved to the disk)
"""

import io
import os
import pathlib
import shutil
import stat

from pd import layout as L
from pd.config import CIPHER_ARGS, Config
from pd.util import Argon2Params

# Kernel command line embedded in the boot image.
BOOT_CMDLINE = "init_on_free=1 loglevel=4"
IMAGE = "BOOTX64.EFI"
CREDENTIALS = ".extra/credentials"


def early_env(argon2: Argon2Params | None = None) -> str:
    """The boot image's early_env.sh."""
    a = argon2 or Argon2Params()
    env = (
        f"PLAIN_OFFSET={L.sectors(L.DECOY_PLAIN_OFFSET)}\n"
        f"PLAIN_SIZE={L.sectors(L.PLAIN_SIZE)}\n"
        f'ARGON2_ARGS="{a.shell()}"\n'
        f'CRYPT_ARGS="{" ".join(CIPHER_ARGS)}"\n'
    )
    # The hidden boot replaces the first "2048" on each line with sed; it
    # must appear exactly once, in PLAIN_OFFSET.
    assert env.count("2048") == 1, env
    return env


def credentials(cfg: Config, keymaps: pathlib.Path | None = None) -> dict[str, bytes]:
    """The per-install files for cfg, by name (without the .cred suffix)."""
    out = {}
    if cfg.salt:
        out["salt"] = cfg.salt.encode()
    if cfg.argon2.shell() != Argon2Params().shell():
        out["argon2"] = cfg.argon2.shell().encode()
    if cfg.keymap:
        if keymaps is None:
            raise ValueError("a keymap needs --env (or --keymaps) to find the keymap files")
        bmap = pathlib.Path(keymaps) / f"{cfg.keymap}.bmap"
        if not bmap.exists():
            raise ValueError(f"unknown keymap {cfg.keymap!r} (see {bmap.parent})")
        out["keymap"] = bmap.read_bytes()
    return out


def _cpio_entry(name: str, data: bytes, mode: int, ino: int) -> bytes:
    namez = name.encode() + b"\0"
    hdr = "070701" + "".join(f"{v:08x}" for v in (
        ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(namez), 0))
    out = hdr.encode() + namez
    out += b"\0" * (-len(out) % 4)
    out += data
    out += b"\0" * (-len(out) % 4)
    return out


def install_cpio(cfg: Config, env: pathlib.Path | None = None) -> bytes:
    """The per-install files as the cpio systemd-stub would pass to the
    initrd, for booting the boot image's kernel and initrd directly (tests,
    QEMU's -kernel/-initrd)."""
    entries = [(".extra", b"", stat.S_IFDIR | 0o555), (CREDENTIALS, b"", stat.S_IFDIR | 0o500)]
    keymaps = pathlib.Path(env) / "keymaps" if env else None
    for name, data in sorted(credentials(cfg, keymaps).items()):
        entries.append((f"{CREDENTIALS}/{name}.cred", data, stat.S_IFREG | 0o400))
    buf = io.BytesIO()
    for i, (name, data, mode) in enumerate(entries, start=1):
        buf.write(_cpio_entry(name, data, mode, i))
    buf.write(_cpio_entry("TRAILER!!!", b"", 0, 0))
    return buf.getvalue()


def write_usb(cfg: Config | None, image: pathlib.Path, esp: pathlib.Path,
              keymaps: pathlib.Path | None = None) -> list[str]:
    """Put the boot image and cfg's per-install files on a mounted ESP.
    Returns the files written, relative to it."""
    boot = esp / "EFI" / "BOOT"
    boot.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image, boot / IMAGE)
    written = [f"EFI/BOOT/{IMAGE}"]
    extra = boot / f"{IMAGE}.extra.d"
    if extra.is_dir():
        for f in extra.glob("*.cred"):
            f.unlink()
    files = credentials(cfg, keymaps) if cfg else {}
    if files:
        extra.mkdir(exist_ok=True)
        for name, data in sorted(files.items()):
            (extra / f"{name}.cred").write_bytes(data)
            written.append(f"EFI/BOOT/{IMAGE}.extra.d/{name}.cred")
    elif extra.is_dir() and not any(extra.iterdir()):
        extra.rmdir()
    os.sync()
    return written


def read_member(uki: pathlib.Path, member: str) -> str | None:
    """A file of the per-install cpio in an old boot image (from before the
    salt moved to the disk), or None.  That cpio is stored uncompressed, so
    the file can be found by its newc header."""
    data = pathlib.Path(uki).read_bytes()
    name = member.encode() + b"\0"
    i = data.find(name)
    while i != -1:
        hdr = data[i - 110:i]
        if hdr.startswith(b"070701") and int(hdr[94:102], 16) == len(name):
            size = int(hdr[54:62], 16)
            start = i + len(name)
            start += -(start - (i - 110)) % 4
            return data[start:start + size].decode()
        i = data.find(name, i + 1)
    return None


def read_salt(uki: pathlib.Path) -> str:
    """The salt in an old boot image."""
    salt = read_member(uki, "salt.txt")
    if salt is None:
        raise ValueError(f"no salt.txt found in {uki}")
    return salt


def build_uki(env: pathlib.Path, out: pathlib.Path, cmdline: str = BOOT_CMDLINE) -> None:
    """The boot image with another kernel command line (the tests' serial
    console).  With BOOT_CMDLINE it's the release image (nix bootUki)."""
    from pd.util import cmd
    cmd(env / "ukify" / "bin" / "ukify", "build",
        "--linux", env / "bzImage",
        "--initrd", env / "boot-initrd.cpio.zst",
        "--stub", env / "stub.efi",
        "--cmdline", cmdline,
        # ukify defaults to the build host's /usr/lib/os-release, which
        # would record the host OS on the USB.
        "--os-release", "NAME=Linux\nID=linux\n",
        "--output", out)
