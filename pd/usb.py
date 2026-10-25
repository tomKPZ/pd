"""Build the boot USB's per-install files and UKI."""

import io
import os
import pathlib
import stat

from pd import layout as L
from pd.config import CIPHER_ARGS, Config

# Kernel command line embedded in the UKI.
BOOT_CMDLINE = "init_on_free=1 loglevel=4"


def early_env(cfg: Config) -> str:
    env = (
        f"PLAIN_OFFSET={L.sectors(L.DECOY_PLAIN_OFFSET)}\n"
        f"PLAIN_SIZE={L.sectors(L.PLAIN_SIZE)}\n"
        f'ARGON2_ARGS="{cfg.argon2.shell()}"\n'
        f'CRYPT_ARGS="{" ".join(CIPHER_ARGS)}"\n'
    )
    # The hidden boot replaces the first "2048" on each line with sed; it
    # must appear exactly once, in PLAIN_OFFSET.
    assert env.count("2048") == 1, env
    return env


def _cpio_entry(name: str, data: bytes, mode: int, ino: int) -> bytes:
    namez = name.encode() + b"\0"
    hdr = "070701" + "".join(f"{v:08x}" for v in (
        ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(namez), 0))
    out = hdr.encode() + namez
    out += b"\0" * (-len(out) % 4)
    out += data
    out += b"\0" * (-len(out) % 4)
    return out


def install_cpio(cfg: Config) -> bytes:
    """newc cpio with the per-install files: salt.txt and early_env.sh."""
    buf = io.BytesIO()
    files = [("salt.txt", cfg.salt.encode()), ("early_env.sh", early_env(cfg).encode())]
    for i, (name, data) in enumerate(files, start=1):
        buf.write(_cpio_entry(name, data, stat.S_IFREG | 0o644, i))
    buf.write(_cpio_entry("TRAILER!!!", b"", 0, 0))
    return buf.getvalue()


def build_uki(cfg: Config, env: pathlib.Path, out: pathlib.Path, cmdline: str = BOOT_CMDLINE) -> None:
    from pd.util import cmd
    cpio = out.with_suffix(".install.cpio")
    cpio.write_bytes(install_cpio(cfg))
    try:
        cmd(env / "ukify" / "bin" / "ukify", "build",
            "--linux", env / "bzImage",
            "--initrd", env / "boot-initrd.cpio.zst",
            "--initrd", cpio,
            "--stub", env / "stub.efi",
            "--cmdline", cmdline,
            "--output", out)
    finally:
        os.unlink(cpio)

