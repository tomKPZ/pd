"""Disk setup: randomize, create the decoy side, create the hidden side.

Runs as root in a throwaway environment (see docs/design.md §9).  Keys only
travel through pipes and Python memory; the only files they are written to
are inside the encrypted boot volumes, where they belong.
"""

import contextlib
import dataclasses
import json
import os
import pathlib
import shutil
import subprocess

from pd import fido2
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


def ram_size() -> int:
    """This machine's RAM, rounded up to a whole GiB (MemTotal is a little less
    than what's installed)."""
    with open("/proc/meminfo") as f:
        kib = next(int(line.split()[1]) for line in f if line.startswith("MemTotal:"))
    return -(-kib * 1024 // L.GIB) * L.GIB


def swap_size(cfg: Config) -> int:
    return ram_size() if cfg.swap_size is None else cfg.swap_size


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
    """Zero the first and the last MiB (L.ZERO_HEADER, L.ZERO_TRAILER)."""
    cmd("dd", "if=/dev/zero", f"of={disk}", "bs=1M", "count=1", "conv=fsync", "status=none")
    end = L.disk_size(device_size(disk))
    cmd("dd", "if=/dev/zero", f"of={disk}", "bs=1M", "count=1", f"seek={end // L.MIB - 1}",
        "conv=fsync", "status=none")


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


def install_hidden_extras(mnt: pathlib.Path, cfg: Config, decoy_key: bytes, decoy_table: str,
                          frontier_env: str = "") -> None:
    """Hidden-only files: never copy these into the decoy."""
    write_file(mnt / "decoy.key", decoy_key, 0o600)
    write_file(mnt / "decoy.table", decoy_table)
    (mnt / "hooks").mkdir()
    shutil.copy(REPO / "plain" / "hidden" / "frontier.sh", mnt / "hooks" / "frontier.sh")
    write_file(mnt / "frontier.env", f"MIN_GAP_PERCENT={cfg.min_gap_percent}\n{frontier_env}")
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
    swap = swap_size(cfg)
    if swap:
        lvm("lvcreate", "-qq", "-L", f"{swap}b", "-n", "swap", cfg.vg)
    if front_metadata:
        # Create the pool with one extent, then extend it: this puts tmeta
        # and pmspare at the front of the PV, far from where the two sides
        # can meet.  (Stock placement would put them at the frontier.)
        lvm("lvcreate", "-qq", "--type", "thin-pool", "-l", "1", "--discards", "nopassdown", "-Zn",
            "-n", "thinpool", cfg.vg)
        lvm("lvextend", "-qq", "-l", "+100%FREE", f"{cfg.vg}/thinpool")
    else:
        # -Zn: don't zero new chunks (LVM suggests it for large chunks; the
        # filesystems never read blocks they haven't written).
        lvm("lvcreate", "-qq", "--type", "thin-pool", "-l", "100%FREE", "--discards", "nopassdown", "-Zn",
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
    if swap:
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
    data, or the end of the decoy's data region (D - 1 MiB) if there is none."""
    def phys(extent: int, ext: int) -> int:
        return data_start + pe_start + extent * ext

    tdata = [s for s in segments if s["lv"].endswith("_tdata")]
    tdata_end = max(phys(s["last"] + 1, s["extent"]) for s in tdata)
    after = [phys(s["first"], s["extent"]) for s in segments
             if not s["lv"].endswith("_tdata") and phys(s["first"], s["extent"]) >= tdata_end]
    return min(after) if after else L.data_end(disk_bytes)


def check_no_overlap(segments: list[dict], pe_start: int, hl: L.HiddenLayout,
                     data_start: int = L.DATA_START) -> None:
    """The hidden side may only share space with the decoy pool's data.

    Decoy metadata at the front (PV header, swap, and on v1-style layouts the
    pool metadata) lies below the decoy's thin data and is only reachable by
    a hidden overflow, which would already have destroyed decoy data.  Decoy
    LVs must never intersect the hidden boot volume or the upper chunks of the
    hidden data region.  `data_start` is where the decoy's PV starts on the disk.
    """
    def rng(s):
        ext = s["extent"]
        return (data_start + pe_start + s["first"] * ext,
                data_start + pe_start + (s["last"] + 1) * ext)

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
    """Does key open a boot volume at offset?  Quiet: wrong keys are expected
    when trying candidates (find_boot_key)."""
    try:
        with plain_crypt(BOOT_MAP, disk, key, CIPHER_ARGS, offset=L.sectors(offset),
                         size=L.sectors(L.PLAIN_SIZE), readonly=True) as dev:
            with mountpoint("boot-") as mnt:
                if cmd("mount", "-t", "btrfs", "-o", "ro,nodiscard,rescue=nologreplay", dev, mnt,
                       check=False, stderr=subprocess.DEVNULL).returncode != 0:
                    return False
                try:
                    return (mnt / "data.key").exists()
                finally:
                    cmd("umount", mnt)
    except subprocess.CalledProcessError:
        return False


def find_boot_key(cfg: Config, offset: int, password: bytes) -> tuple[bytes, int | None] | None:
    """The key that opens the boot volume at offset, and the FIDO2 slot it
    came from (None: the password alone, A = Argon2(password, salt)); None
    if nothing opens it.  Asks a present security key if FIDO2 is set up."""
    a = argon2(password, cfg.salt, cfg.argon2)
    for key, slot in fido2.candidate_keys(cfg, a):
        if boot_volume_exists(cfg.disk, offset, key):
            return key, slot
    return None


def boot_key(cfg: Config, offset: int, password: bytes, what: str = "that password") -> bytes:
    found = find_boot_key(cfg, offset, password)
    if found is None:
        hint = " (or no enrolled security key is plugged in)" if fido2.load(cfg) else ""
        raise RuntimeError(f"{what} doesn't open a boot volume at that offset{hint}")
    return found[0]


def read_hidden_layout(cfg: Config, offset: int, password: bytes) -> L.HiddenLayout:
    """The layout of an existing hidden side, from its boot volume."""
    key = boot_key(cfg, offset, password, "the hidden password")
    with boot_volume(cfg.disk, offset, key, create=False) as mnt:
        table = (mnt / "table.txt").read_text()
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
    existing = find_boot_key(cfg, L.DECOY_PLAIN_OFFSET, password)
    # Re-creating keeps the existing key, so FIDO2 enrollment carries over.
    key = existing[0] if existing else argon2(password, cfg.salt, cfg.argon2)
    if not force and existing:
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
    table = L.linear_table(L.DATA_START, L.data_end(d))
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
        key = boot_key(cfg, L.DECOY_PLAIN_OFFSET, password, "the decoy password")
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
    create_hidden(cfg, d, hl, key, decoy_key, decoy_table, "", run)
    return hl


def create_hidden(cfg: Config, d: int, hl: L.HiddenLayout, key: bytes, decoy_key: bytes,
                  decoy_table: str, frontier_env: str, run=None) -> None:
    data_key = os.urandom(64)
    table = hl.data_table()
    with boot_volume(cfg.disk, hl.plain_offset, key, create=True) as mnt:
        write_boot_volume(mnt, cfg, data_key, table)
        install_hidden_extras(mnt, cfg, decoy_key, decoy_table, frontier_env)
    with data_region(cfg.disk, table, data_key, readonly=False) as dev:
        create_lvm(dev, cfg, hidden=True, disk_bytes=d)
        with vg_active(cfg.vg):
            if run:
                with mountpoint("mnt-") as base, roots_mounted(cfg, base):
                    run(base)


# --- Stock decoy (docs/stock-boot.md) -------------------------------------------------
#
# The decoy is an ordinary install: LUKS (attached or detached header) holding
# LVM with a thin pool.  pd only ever opens it read-only, to find where its
# pool's data lies and to give the frontier monitor its key.

STOCK_MAP = "pd-stock-decoy"

# dm-crypt options that don't change what's on the disk.
CRYPT_PERF_OPTS = {"same_cpu_crypt", "submit_from_crypt_cpus", "no_read_workqueue",
                   "no_write_workqueue", "high_priority"}


@dataclasses.dataclass
class StockDecoy:
    key: bytes              # the LUKS volume key
    table: str              # the LUKS data area, as a linear table on DISK
    crypt_args: list[str]   # plain-mode arguments that read it like the LUKS mapping
    vg: str
    pv_start: int           # disk offset of the LUKS data area, where the PV starts
    segments: list[dict]
    pe_start: int


def crypt_plain_args(line: str) -> tuple[bytes, int, int, list[str]]:
    """Parse a dm-crypt table line (`dmsetup table --showkeys`) into (key,
    data offset in sectors, length in sectors, plain-mode cryptsetup
    arguments that reproduce the mapping)."""
    f = line.split()
    if len(f) < 8 or f[2] != "crypt":
        raise RuntimeError("the decoy's mapping isn't dm-crypt")
    length, cipher, key, iv_offset, offset = int(f[1]), f[3], f[4], int(f[5]), int(f[7])
    if key.startswith(":"):
        raise RuntimeError("the decoy's volume key is in the kernel keyring and can't be read")
    args = [f"--cipher={cipher}", f"--key-size={len(key) * 4}"]
    for opt in f[9:9 + int(f[8])] if len(f) > 8 else []:
        if opt.startswith("sector_size:"):
            args.append(f"--sector-size={opt.split(':', 1)[1]}")
        elif opt == "iv_large_sectors":
            args.append("--iv-large-sectors")
        elif opt == "allow_discards":
            raise RuntimeError(
                "the decoy's LUKS volume allows discards (a persistent flag), which reveals the disk's "
                "used blocks.  On the decoy, clear it with `cryptsetup refresh --persistent NAME` "
                "(without --allow-discards); see docs/stock-boot.md")
        elif opt not in CRYPT_PERF_OPTS:
            raise RuntimeError(f"unsupported dm-crypt option on the decoy: {opt}")
    if iv_offset:
        args.append(f"--skip={iv_offset}")
    return bytes.fromhex(key), offset, length, args


def partition_offset(device: str, disk: str) -> int:
    """Byte offset of `device` on `disk`: 0 for the disk itself, else the
    partition's start."""
    dev = pathlib.Path(os.path.realpath(device)).name
    whole = pathlib.Path(os.path.realpath(disk)).name
    if dev == whole:
        return 0
    sysfs = pathlib.Path("/sys/class/block") / dev
    if not (sysfs / "partition").exists() or sysfs.resolve().parent.name != whole:
        raise RuntimeError(f"{device} is neither {disk} nor one of its partitions")
    return int((sysfs / "start").read_text()) * L.SECTOR


def luks_uuid(device: str, header: str | None) -> str:
    return cmd_output("cryptsetup", "luksUUID", header or device)


@contextlib.contextmanager
def stock_decoy_opened(device: str, password: bytes, header: str | None):
    """The decoy's LUKS volume, read-only, with the key in the dm table
    (not the kernel keyring) so it can be read back."""
    r, w = os.pipe()
    try:
        os.write(w, password)
        os.close(w)
        try:
            cmd("cryptsetup", "open", "--readonly", "--disable-keyring", "--batch-mode",
                *([f"--header={header}"] if header else []), f"--key-file=/proc/self/fd/{r}",
                device, STOCK_MAP, pass_fds=(r,))
        except subprocess.CalledProcessError:
            raise RuntimeError(f"the decoy password doesn't open a LUKS volume on {device}")
    finally:
        os.close(r)
    try:
        yield pathlib.Path("/dev/mapper") / STOCK_MAP
    finally:
        cmd("cryptsetup", "close", STOCK_MAP)


def check_plain_equivalent(table: str, key: bytes, args: list[str], luks_dev: pathlib.Path) -> None:
    """The frontier monitor opens the decoy in plain mode: make sure that
    reads the same as the LUKS mapping.  `table` maps the LUKS data area
    relative to the LUKS device: while the LUKS mapping holds a partition,
    the kernel refuses mappings of the whole disk."""
    with dm_device("pd-stock-raw", table, readonly=True) as raw, \
            plain_crypt("pd-stock-plain", raw, key, args, readonly=True) as plain:
        with open(luks_dev, "rb") as a, open(plain, "rb") as b:
            if a.read(4 * L.MIB) != b.read(4 * L.MIB):
                raise RuntimeError("can't reproduce the decoy's encryption in plain mode "
                                   "(the frontier monitor needs it)")


def read_stock_decoy(cfg: Config, device: str, password: bytes, header: str | None) -> StockDecoy:
    start = partition_offset(device, cfg.disk)
    with stock_decoy_opened(device, password, header) as dev:
        key, offset, length, args = crypt_plain_args(
            cmd_output("dmsetup", "table", "--showkeys", STOCK_MAP))
        vg = lvm_output("pvs", "--noheadings", "-o", "vg_name", dev)
        if not vg:
            raise RuntimeError(f"no LVM volume group inside the LUKS volume on {device}")
        segs = lvm_segments(vg)
        pe_start = pv_data_offset(str(dev))
        pv_start = start + offset * L.SECTOR
        table = L.linear_table(pv_start, pv_start + length * L.SECTOR)
        # No LVM commands from here on: the plain mapping shows the same PV twice.
        check_plain_equivalent(L.linear_table(offset * L.SECTOR, (offset + length) * L.SECTOR,
                                              dev=device), key, args, dev)
    return StockDecoy(key=key, table=table, crypt_args=args, vg=vg, pv_start=pv_start,
                      segments=segs, pe_start=pe_start)


def stock_hidden_layout(cfg: Config, sd: StockDecoy) -> L.HiddenLayout:
    """The hidden side goes at the top of the decoy pool's data, and its data
    region stays within it."""
    tdata = [s for s in sd.segments if s["lv"].endswith("_tdata")]
    if len(tdata) != 1:
        raise RuntimeError(f"volume group {sd.vg} needs exactly one thin pool, with its data in one "
                           "contiguous segment (as on a fresh install)")
    seg = tdata[0]
    lo = sd.pv_start + sd.pe_start + seg["first"] * seg["extent"]
    hi = sd.pv_start + sd.pe_start + (seg["last"] + 1) * seg["extent"]
    hl = L.hidden_layout(hi, memorable=cfg.memorable_offset, bottom=lo)
    check_no_overlap(sd.segments, sd.pe_start, hl, data_start=sd.pv_start)
    return hl


def init_hidden_stock(cfg: Config, device: str, header: str | None, decoy_password: bytes,
                      password: bytes, run=None) -> L.HiddenLayout:
    """Create the hidden side next to a stock decoy whose LUKS volume is on
    `device` (cfg.disk or one of its partitions)."""
    if decoy_password == password:
        raise RuntimeError("the hidden password must differ from the decoy password")
    set_hostname(cfg)
    d = check_disk(cfg.disk)
    sd = read_stock_decoy(cfg, device, decoy_password, header)
    hl = stock_hidden_layout(cfg, sd)
    key = argon2(password, cfg.salt, cfg.argon2)
    frontier_env = f'DECOY_VG={sd.vg}\nDECOY_CRYPT_ARGS="{" ".join(sd.crypt_args)}"\n'
    create_hidden(cfg, d, hl, key, sd.key, sd.table, frontier_env, run)
    return hl


def stock_boot_commands(cfg: Config, hl: L.HiddenLayout, salt_expr: str) -> list[str]:
    """What to type, as root, in a live system to boot the hidden side
    (docs/stock-boot.md).  `salt_expr` is the salt as typed, e.g.
    "$(cryptsetup luksUUID /dev/nvme0n1p2)"."""
    a = cfg.argon2
    kdf = (f'openssl kdf -binary -keylen {a.length} -kdfopt pass:"$P" -kdfopt salt:{salt_expr} '
           f'-kdfopt iter:{a.t} -kdfopt memcost:{1 << a.m_log2} -kdfopt lanes:{a.p} ARGON2ID')
    short = {"--cipher": "-c", "--key-size": "-s"}
    crypt = " ".join(" ".join((short.get(k, k), v)) for k, v in (arg.split("=", 1) for arg in CIPHER_ARGS))
    return [
        "read -rs P",
        f"cryptsetup open -r --type plain {crypt} -o {hl.plain_offset_sectors} -d <({kdf}) "
        f"{cfg.disk} cryptboot",
        "unset P",
        f"mkdir -p /mnt/boot && mount -o ro /dev/mapper/cryptboot /mnt/boot && /mnt/boot/late.sh {cfg.disk}",
    ]
