"""Migrate a v1 install to v2 (docs/migration.md).

Per side: derive the v1 key, read the v1 boot volume (LUKS header, data
table, systems/), extract the LUKS volume key, verify that plain dm-crypt
with it shows the same LVM PV, then re-create the boot volume in place under
the v2 key with v2 contents.  The data region is not touched.
"""

import dataclasses
import os
import pathlib
import shutil
import subprocess
import tempfile

from pd import layout as L
from pd import header, setup
from pd.config import CIPHER_ARGS, Config
from pd.util import Argon2Params, argon2, cmd, cmd_output, mounted, mountpoint, plain_crypt, write_file

V1_ARGON2 = Argon2Params(t=14, m_log2=18, p=8, length=512)
V1_DISK_PATH = "/boot_tmp/disk"


@dataclasses.dataclass
class V1Side:
    data_key: bytes
    table: str  # with DISK placeholder
    systems: dict[str, dict[str, bytes]]  # name -> {filename: content}
    default: str | None


def v1_key(password: bytes, salt: str, hidden: bool, params: Argon2Params) -> bytes:
    return argon2(password, salt + ("a" if hidden else ""), params)


def read_v1_side(cfg: Config, offset: int, key: bytes) -> V1Side:
    with plain_crypt(setup.BOOT_MAP, cfg.disk, key[:64], CIPHER_ARGS, offset=L.sectors(offset),
                     size=L.sectors(L.PLAIN_SIZE), readonly=True) as dev, \
            mountpoint("v1boot-") as mnt, \
            mounted(dev, mnt, "btrfs", "ro,nodiscard,rescue=nologreplay"):
        header = (mnt / "header.dat").read_bytes()
        table = (mnt / "table.txt").read_text().replace(V1_DISK_PATH, "DISK")
        systems: dict[str, dict[str, bytes]] = {}
        default = None
        sdir = mnt / "systems"
        if sdir.is_dir():
            for d in sorted(sdir.iterdir()):
                if d.is_symlink():
                    if d.name == "default":
                        default = os.readlink(d)
                    continue
                systems[d.name] = {f.name: f.read_bytes() for f in d.iterdir() if f.is_file()}
    data_key = extract_volume_key(header, key[64:])
    return V1Side(data_key=data_key, table=table, systems=systems, default=default)


def extract_volume_key(header: bytes, passphrase: bytes) -> bytes:
    """Dump the LUKS2 volume key.  Header, passphrase and key only touch
    tmpfs (/run), and are removed afterwards."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pd-migrate-", dir="/run"))
    try:
        hdr, pw, vk = tmp / "header", tmp / "pw", tmp / "vk"
        write_file(hdr, header, 0o600)
        write_file(pw, passphrase, 0o600)
        cmd("cryptsetup", "luksDump", "--batch-mode", "--dump-volume-key",
            f"--volume-key-file={vk}", f"--key-file={pw}", hdr, stdout=subprocess.DEVNULL)
        return vk.read_bytes()
    finally:
        shutil.rmtree(tmp)


def verify_data_region(cfg: Config, side: V1Side) -> None:
    with setup.data_region(cfg.disk, side.table, side.data_key, readonly=True) as dev:
        with open(dev, "rb") as f:
            f.seek(512)
            if f.read(8) != b"LABELONE":
                raise RuntimeError("the extracted volume key doesn't show an LVM PV")


def write_v2_side(cfg: Config, offset: int, key: bytes, side: V1Side,
                  decoy: V1Side | None = None) -> None:
    with setup.boot_volume(cfg.disk, offset, key, create=True) as mnt:
        setup.write_boot_volume(mnt, cfg, side.data_key, side.table,
                                systems_from=side.systems, default=side.default)
        if decoy is not None:
            setup.install_hidden_extras(mnt, cfg, decoy.data_key, decoy.table)


def migrate(cfg: Config, v1_salt: str, decoy_password: bytes, hidden_password: bytes | None,
            hidden_offset: int | None, v1_params: Argon2Params = V1_ARGON2) -> None:
    """hidden_offset is in bytes."""
    # Read and verify everything before writing anything.
    decoy = read_v1_side(cfg, L.DECOY_PLAIN_OFFSET, v1_key(decoy_password, v1_salt, False, v1_params))
    verify_data_region(cfg, decoy)
    hidden = None
    if hidden_password is not None:
        if hidden_password == decoy_password:
            raise RuntimeError("the hidden password must differ from the decoy password")
        hidden = read_v1_side(cfg, hidden_offset, v1_key(hidden_password, v1_salt, True, v1_params))
        verify_data_region(cfg, hidden)

    if not cfg.salt and header.disk_class(cfg.disk) != "plain":
        # The salt moves to the disk: v1's first MiB is zeros and unused.
        # Only the header is written; v1's decoy data region reaches the end
        # of the disk, so the trailer stays as it is.  (A header from an
        # interrupted earlier run is kept.)
        with open(cfg.disk, "rb") as f:
            if f.read(L.HEADER) != bytes(L.HEADER):
                raise RuntimeError(f"{cfg.disk}'s first MiB isn't all zeros; refusing to overwrite it")
        if hidden is not None:
            header.check_header_clear(cfg, hidden.table, hidden.data_key)
        header.write_edges(cfg.disk, trailer=False)
    salt = header.salt(cfg)
    write_v2_side(cfg, L.DECOY_PLAIN_OFFSET, argon2(decoy_password, salt, cfg.argon2), decoy)
    if hidden is not None:
        write_v2_side(cfg, hidden_offset, argon2(hidden_password, salt, cfg.argon2), hidden,
                      decoy=decoy)
