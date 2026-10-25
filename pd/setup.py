"""Disk setup: randomize, create the decoy side, create the hidden side.

Runs as root in a throwaway environment (see docs/design.md §9).  Keys only
travel through pipes and Python memory; the only files they are written to
are inside the encrypted boot volumes, where they belong.
"""

import contextlib
import json
import os
import pathlib
import shutil
import subprocess

from pd import layout as L
from pd.config import CIPHER_ARGS, Config
from pd.util import argon2, cmd, cmd_output, dm_device, mounted, mountpoint, plain_crypt, write_file

REPO = pathlib.Path(__file__).resolve().parent.parent
LVM_NO_ARCHIVE = "backup { backup = 0 archive = 0 }"

# Mapping names.  The data-region names are the ones the target OS hook
# uses, because LVM records the PV's device path in its metadata.
BOOT_MAP = "cryptboot"
RAW_MAP = "cryptlvm-raw"
DATA_MAP = "cryptlvm"


def lvm(*args, **kwargs):
    return cmd("lvm", *args, "--config", LVM_NO_ARCHIVE, **kwargs)


def lvm_output(*args) -> str:
    return cmd_output("lvm", *args, "--config", LVM_NO_ARCHIVE)


def device_size(dev) -> int:
    return int(cmd_output("blockdev", "--getsize64", dev))


# --- Randomize ---------------------------------------------------------------

def randomize(disk: str) -> None:
    """Fill the disk with random-looking data and zero the first MiB.

    cryptsetup reads the throwaway key from /dev/urandom itself, so it never
    exists in userspace.
    """
    cmd("cryptsetup", "open", "--type=plain", "--batch-mode", *CIPHER_ARGS,
        "--key-file=/dev/urandom", disk, "pd-randomize")
    try:
        cmd("dd", "if=/dev/zero", "of=/dev/mapper/pd-randomize", "bs=4M",
            "oflag=direct", "status=progress", check=False)
    finally:
        cmd("cryptsetup", "close", "pd-randomize")
    zero_header(disk)


def zero_header(disk: str) -> None:
    cmd("dd", "if=/dev/zero", f"of={disk}", "bs=1M", "count=1", "conv=fsync", "status=none")


# --- Boot volume ---------------------------------------------------------------

@contextlib.contextmanager
def boot_volume(disk: str, offset: int, key: bytes, *, create: bool):
    """Open (and optionally create) a side's encrypted boot volume."""
    with plain_crypt(BOOT_MAP, disk, key, CIPHER_ARGS, offset=L.sectors(offset),
                     size=L.sectors(L.PLAIN_SIZE), readonly=not create) as dev:
        if create:
            cmd("mkfs.btrfs", "--quiet", "--force", "--nodiscard", "--data=dup",
                "--metadata=dup", "--csum=blake2", dev)
        opts = "noatime,nodiscard,compress=zstd:15" if create else "ro,nodiscard,rescue=nologreplay"
        with mountpoint("boot-") as mnt, mounted(dev, mnt, "btrfs", opts):
            yield mnt


def write_boot_volume(mnt: pathlib.Path, cfg: Config, data_key: bytes, table: str,
                      extra: dict[str, bytes] | None = None,
                      systems_from: dict[str, dict[str, bytes]] | None = None,
                      default: str | None = None) -> None:
    """Fill a freshly created boot volume.

    `systems_from` (used by migration) supplies existing systems/<name>/
    files instead of generating env.sh from the config; their extra.sh is
    replaced by the current one from plain/systems/<name>/ if there is one.
    """
    tree = pathlib.Path(os.environ.get("PD_PLAIN_TREE", ""))
    if not (tree / "late.sh").exists():
        raise RuntimeError("PD_PLAIN_TREE must point at the built plain tree (nix build .#plainTree)")
    shutil.copytree(tree / "bin", mnt / "bin", symlinks=True)
    for f in ("late.sh", "thin-ro.sh"):
        shutil.copy(tree / f, mnt / f)
    os.chmod(mnt / "late.sh", 0o755)
    write_file(mnt / "data.key", data_key, 0o600)
    write_file(mnt / "table.txt", table)
    write_file(mnt / "crypt.env", f'CRYPT_ARGS="{" ".join(CIPHER_ARGS)}"\n')
    systems = mnt / "systems"
    systems.mkdir()
    if systems_from is None:
        systems_from = {}
        for s in cfg.systems:
            systems_from[s.name] = {"env.sh": "".join(f"{k}='{v}'\n" for k, v in [
                ("VG", cfg.vg), ("LV_ROOT", s.lv), ("KERNEL", s.kernel),
                ("INITRD", s.initrd), ("CMDLINE", s.cmdline)]).encode()}
        extras = {s.name: s.extra or s.name for s in cfg.systems}
        default = cfg.default
    else:
        extras = {name: name for name in systems_from}
    for name, files in systems_from.items():
        d = systems / name
        d.mkdir()
        write_file(d / "env.sh", files["env.sh"])
        hook = REPO / "plain" / "systems" / extras[name] / "extra.sh"
        if hook.exists():
            shutil.copy(hook, d / "extra.sh")
            os.chmod(d / "extra.sh", 0o755)
    if default:
        os.symlink(default, systems / "default")
    for name, data in (extra or {}).items():
        write_file(mnt / name, data, 0o600)
    cmd("sync")


def install_hidden_extras(mnt: pathlib.Path, cfg: Config, decoy_key: bytes, decoy_table: str) -> None:
    """Hidden-only files: never copy these into the decoy."""
    write_file(mnt / "decoy.key", decoy_key, 0o600)
    write_file(mnt / "decoy.table", decoy_table)
    (mnt / "hooks").mkdir()
    shutil.copy(REPO / "plain" / "hidden" / "frontier.sh", mnt / "hooks" / "frontier.sh")
    write_file(mnt / "frontier.env", f"MIN_GAP_PERCENT={cfg.min_gap_percent}\n")
    cmd("sync")


# --- Data region -----------------------------------------------------------------

@contextlib.contextmanager
def data_region(disk: str, table: str, data_key: bytes, *, readonly: bool):
    tbl = table.replace("DISK", disk)
    with dm_device(RAW_MAP, tbl, readonly=readonly) as raw, \
            plain_crypt(DATA_MAP, raw, data_key, CIPHER_ARGS, readonly=readonly) as dev:
        yield dev


@contextlib.contextmanager
def vg_active(vg: str, readonly: bool = False):
    args = ["vgchange", "-ay", "--ignoremonitoring", vg]
    if readonly:
        args = ["vgchange", "-ay", "--ignoremonitoring", "--config",
                f'activation {{ read_only_volume_list = [ "{vg}" ] }}', vg]
    lvm(*args)
    try:
        yield
    finally:
        lvm("vgchange", "-an", vg)


def create_root_fs(dev: str, cfg: Config) -> None:
    cmd("mkfs.btrfs", "--quiet", "--force", "--nodiscard", "--csum=xxhash", dev)
    with mountpoint("root-") as mnt, mounted(dev, mnt, "btrfs", "nodiscard,subvolid=5"):
        for name in cfg.subvolumes:
            cmd("btrfs", "subvolume", "create", mnt / name, stdout=subprocess.DEVNULL)


ROOT_OPTS = "noatime,lazytime,nodiscard,compress=zstd:3"


VOLUME_OPTS = {"btrfs": ROOT_OPTS, "xfs": "noatime,lazytime,nodiscard"}


@contextlib.contextmanager
def roots_mounted(cfg: Config, base: pathlib.Path):
    """Mount every system's root (all subvolumes) under base/<system>, and
    extra volumes under base/volumes/<name>."""
    with contextlib.ExitStack() as stack:
        for v in cfg.volumes:
            target = base / "volumes" / v.name
            target.mkdir(parents=True, exist_ok=True)
            stack.enter_context(mounted(f"/dev/{cfg.vg}/{v.name}", target, v.fs, VOLUME_OPTS[v.fs]))
        for s in cfg.systems:
            dev = f"/dev/{cfg.vg}/{s.lv}"
            root = base / s.name
            for name, path in cfg.subvolumes.items():
                target = root / path
                target.mkdir(parents=True, exist_ok=True)
                stack.enter_context(mounted(dev, target, "btrfs", f"{ROOT_OPTS},subvol={name}"))
        yield base


def create_lvm(dev: str, cfg: Config, *, hidden: bool, disk_bytes: int,
               front_metadata: bool | None = None) -> None:
    try:
        _create_lvm(dev, cfg, hidden=hidden, disk_bytes=disk_bytes,
                    front_metadata=hidden if front_metadata is None else front_metadata)
    except BaseException:
        # Deactivate whatever was created, so the crypt mapping can close.
        lvm("vgchange", "-an", cfg.vg, check=False)
        raise


def _create_lvm(dev: str, cfg: Config, *, hidden: bool, disk_bytes: int,
                front_metadata: bool) -> None:
    lvm("pvcreate", "-qq", dev)
    lvm("vgcreate", "-qq", cfg.vg, dev)
    lvm("lvcreate", "-qq", "-L", f"{cfg.swap_size}b", "-n", "swap", cfg.vg)
    if front_metadata:
        # Create the pool with one extent, then extend it: this puts tmeta
        # and pmspare at the front of the PV, far from where the two sides
        # can meet.  (Stock placement would put them at the frontier.)
        lvm("lvcreate", "-qq", "--type", "thin-pool", "-l", "1", "--discards", "nopassdown",
            "-n", "thinpool", cfg.vg)
        lvm("lvextend", "-qq", "-l", "+100%FREE", f"{cfg.vg}/thinpool")
    else:
        lvm("lvcreate", "-qq", "--type", "thin-pool", "-l", "100%FREE", "--discards", "nopassdown",
            "-n", "thinpool", cfg.vg)
    pool = int(lvm_output("lvs", "--noheadings", "--units", "b", "--nosuffix", "-o", "lv_size",
                          f"{cfg.vg}/thinpool"))
    if hidden:
        # Hard cap on the hidden footprint (see design.md §6.2).
        per_lv = int(disk_bytes * cfg.hidden_cap) // (len(cfg.systems) + len(cfg.volumes))
        # LVM rounds sizes up to whole extents; round down first so the
        # total stays within the cap.
        extent = int(lvm_output("vgs", "--noheadings", "--units", "b", "--nosuffix",
                                "-o", "vg_extent_size", cfg.vg))
        size = min(pool, L.align_down(per_lv, extent))
    else:
        size = pool
    for name in [s.lv for s in cfg.systems] + [v.name for v in cfg.volumes]:
        lvm("lvcreate", "-qq", "-V", f"{size}b", "-T", f"{cfg.vg}/thinpool", "-n", name)
    cmd("mkswap", "-q", f"/dev/{cfg.vg}/swap")
    for s in cfg.systems:
        create_root_fs(f"/dev/{cfg.vg}/{s.lv}", cfg)
    for v in cfg.volumes:
        dev = f"/dev/{cfg.vg}/{v.name}"
        if v.fs == "btrfs":
            cmd("mkfs.btrfs", "--quiet", "--force", "--nodiscard", "--csum=xxhash", dev)
        else:
            cmd("mkfs.xfs", "-q", "-f", "-K", dev)


# --- Layout read-back ---------------------------------------------------------------

def lvm_segments(vg: str) -> list[dict]:
    """Physical extents of every LV segment in the VG, in PV extents."""
    out = lvm_output("lvs", "-a", "--reportformat", "json", "--units", "b", "--nosuffix",
                     "-o", "lv_name,seg_pe_ranges,vg_extent_size", vg)
    segs = []
    for row in json.loads(out)["report"][0]["lv"]:
        for rng in row["seg_pe_ranges"].split():
            pv, extents = rng.rsplit(":", 1)
            if not pv.startswith("/dev/"):
                continue  # a segment on another LV (e.g. the pool on its _tdata)
            a, b = (int(x) for x in extents.split("-"))
            segs.append({"lv": row["lv_name"].strip("[]"), "first": a, "last": b,
                         "extent": int(row["vg_extent_size"])})
    return segs


def pv_data_offset(dev: str) -> int:
    return int(lvm_output("pvs", "--noheadings", "--units", "b", "--nosuffix", "-o", "pe_start", dev))


def decoy_top(segments: list[dict], pe_start: int, data_start: int, disk_bytes: int) -> int:
    """H: the physical start of the decoy metadata that sits after the pool's
    data, or D - 1 MiB if there is none."""
    def phys(extent: int, ext: int) -> int:
        return data_start + pe_start + extent * ext

    tdata = [s for s in segments if s["lv"].endswith("_tdata")]
    tdata_end = max(phys(s["last"] + 1, s["extent"]) for s in tdata)
    after = [phys(s["first"], s["extent"]) for s in segments
             if not s["lv"].endswith("_tdata") and phys(s["first"], s["extent"]) >= tdata_end]
    return min(after) if after else disk_bytes - L.ALIGN


def check_no_overlap(segments: list[dict], pe_start: int, hl: L.HiddenLayout) -> None:
    """The hidden side may only share space with the decoy pool's data.

    Decoy metadata at the front (PV header, swap, and on v1-style layouts the
    pool metadata) lies below the decoy's thin data and is only reachable by
    a hidden overflow, which would already have destroyed decoy data.  Decoy
    LVs must never intersect the hidden boot volume or the upper chunks of the
    hidden data region.
    """
    def rng(s):
        ext = s["extent"]
        return (L.DATA_START + pe_start + s["first"] * ext,
                L.DATA_START + pe_start + (s["last"] + 1) * ext)

    # The pool's data proper starts at the lowest tdata segment holding at
    # least 10% of it (the one-extent trick leaves a tiny first segment).
    # Only what lies in front of that is decoy front metadata.
    tdata = sorted((s for s in segments if s["lv"].endswith("_tdata")), key=lambda s: s["first"])
    total = sum(s["last"] - s["first"] + 1 for s in tdata)
    bulk = next(s for s in tdata if (s["last"] - s["first"] + 1) * 10 >= total)
    tdata_start = rng(bulk)[0]
    for s in segments:
        if s["lv"].endswith("_tdata"):
            continue
        start, end = rng(s)
        if end <= tdata_start or start >= hl.plain_offset + L.PLAIN_SIZE:
            continue
        raise RuntimeError(f"decoy LV {s['lv']} overlaps the hidden region")


# --- Sides -------------------------------------------------------------------------

def check_disk(disk: str) -> int:
    d = L.disk_size(device_size(disk))
    if d < 2 * L.GIB:
        raise RuntimeError("disk too small")
    return d


def set_hostname(cfg: Config) -> None:
    """LVM records the host name in its metadata; make it the decoy's."""
    if cfg.hostname:
        cmd("hostname", cfg.hostname)


def boot_volume_exists(disk: str, offset: int, key: bytes) -> bool:
    try:
        with boot_volume(disk, offset, key, create=False) as mnt:
            return (mnt / "data.key").exists()
    except subprocess.CalledProcessError:
        return False


def read_hidden_layout(cfg: Config, offset: int, password: bytes) -> L.HiddenLayout:
    """The layout of an existing hidden side, from its boot volume."""
    key = argon2(password, cfg.salt, cfg.argon2)
    try:
        with boot_volume(cfg.disk, offset, key, create=False) as mnt:
            table = (mnt / "table.txt").read_text()
    except subprocess.CalledProcessError:
        raise RuntimeError("the hidden password doesn't open a boot volume at that offset")
    lo, hi = L.table_physical_range(table)
    return L.HiddenLayout(plain_offset=offset, data_start=lo, data_end=hi)


def init_decoy(cfg: Config, password: bytes, run=None, *, force: bool = False,
               hidden: tuple[int, bytes] | str | None = None) -> None:
    """Create the decoy side.

    Re-initializing a decoy (its password already opens a boot volume)
    requires `force`, and then `hidden` must say whether a hidden side exists:
    "none", or (offset in bytes, hidden password).  With a hidden side, the
    decoy pool's metadata goes at the front (not stock placement, which puts
    tmeta at the end of the disk, possibly on the hidden boot volume), and
    the new layout is checked against the hidden side before anything is
    written to the data region.  The disk is never re-randomized.
    """
    set_hostname(cfg)
    disk = cfg.disk
    d = check_disk(disk)
    key = argon2(password, cfg.salt, cfg.argon2)
    if not force and boot_volume_exists(disk, L.DECOY_PLAIN_OFFSET, key):
        raise RuntimeError(
            "this disk already has a decoy side with that password.  To re-create it, pass --force "
            "and either --no-hidden or --hidden-offset (the hidden password is then checked).")
    if force and hidden is None:
        raise RuntimeError("--force needs --no-hidden or --hidden-offset")
    hl = None
    if isinstance(hidden, tuple):
        offset, hidden_password = hidden
        if hidden_password == password:
            raise RuntimeError("the decoy password must differ from the hidden password")
        hl = read_hidden_layout(cfg, offset, hidden_password)
    data_key = os.urandom(64)
    table = L.linear_table(L.DATA_START, d)
    with boot_volume(disk, L.DECOY_PLAIN_OFFSET, key, create=True) as mnt:
        write_boot_volume(mnt, cfg, data_key, table)
    with data_region(disk, table, data_key, readonly=False) as dev:
        create_lvm(dev, cfg, hidden=False, disk_bytes=d, front_metadata=hl is not None)
        if hl is not None:
            check_no_overlap(lvm_segments(cfg.vg), pv_data_offset(dev), hl)
        with vg_active(cfg.vg):
            if run:
                with mountpoint("mnt-") as base, roots_mounted(cfg, base):
                    run(base)


def open_decoy_readonly(cfg: Config, password: bytes):
    """Yield (data_key, table, segments, pe_start) of the decoy, read-only."""
    @contextlib.contextmanager
    def ctx():
        key = argon2(password, cfg.salt, cfg.argon2)
        with boot_volume(cfg.disk, L.DECOY_PLAIN_OFFSET, key, create=False) as mnt:
            data_key = (mnt / "data.key").read_bytes()
            table = (mnt / "table.txt").read_text()
        with data_region(cfg.disk, table, data_key, readonly=True) as dev:
            segs = lvm_segments(cfg.vg)
            pe = pv_data_offset(dev)
            yield data_key, table, segs, pe
    return ctx()


def init_hidden(cfg: Config, decoy_password: bytes, password: bytes, run=None) -> L.HiddenLayout:
    if decoy_password == password:
        raise RuntimeError("the hidden password must differ from the decoy password")
    set_hostname(cfg)
    disk = cfg.disk
    d = check_disk(disk)
    with open_decoy_readonly(cfg, decoy_password) as (decoy_key, decoy_table, segs, pe_start):
        top = decoy_top(segs, pe_start, L.DATA_START, d)
    hl = L.hidden_layout(top, memorable=cfg.memorable_offset)
    check_no_overlap(segs, pe_start, hl)
    key = argon2(password, cfg.salt, cfg.argon2)
    if key == argon2(decoy_password, cfg.salt, cfg.argon2):
        raise RuntimeError("passwords derive the same key")
    data_key = os.urandom(64)
    table = hl.data_table()
    with boot_volume(disk, hl.plain_offset, key, create=True) as mnt:
        write_boot_volume(mnt, cfg, data_key, table)
        install_hidden_extras(mnt, cfg, decoy_key, decoy_table)
    with data_region(disk, table, data_key, readonly=False) as dev:
        create_lvm(dev, cfg, hidden=True, disk_bytes=d)
        with vg_active(cfg.vg):
            if run:
                with mountpoint("mnt-") as base, roots_mounted(cfg, base):
                    run(base)
    return hl
