"""Move both sides to another disk, bigger or smaller (`pd move`,
docs/design.md §10.4).

The old disk is only read.  The new disk gets fresh layouts, built the way
init-decoy and init-hidden build them (the decoy in stock LVM placement, the
hidden side below the decoy's end-of-disk metadata, at a new memorable
offset), with new data keys.  Each thin LV is then copied: only the blocks
its pool has mapped, at the same offsets, so filesystems (and their UUIDs)
come over unchanged and the new pools fill from the front.  Swap is
recreated with the same UUID.  On a bigger disk the thin LVs and their
btrfs/XFS filesystems are grown to the usual sizes.

Cloning the disk instead would leave two images of the same free space,
which is exactly what pd must avoid (docs/threat-model.md).
"""

import contextlib
import dataclasses
import os
import pathlib
import subprocess

from pd import layout as L
from pd import header, rekey, setup, thinpool
from pd.config import Config
from pd.opener import thin_table
from pd.util import argon2, cmd, cmd_output, dm_device, mountpoint

OLD_MAP = "pd-old"
COPY_BUFFER = 16 * L.MIB


@dataclasses.dataclass
class LV:
    name: str
    segtype: str
    size: int                     # bytes
    mapped: int = 0               # bytes the pool has mapped for it (thin LVs)
    fs: str = ""
    uuid: str = ""                # swap UUID
    thin_id: str = ""             # thin LVs
    segments: list = dataclasses.field(default_factory=list)   # linear LVs (thinpool.Segment)


@dataclasses.dataclass
class OldSide:
    offset: int
    password: bytes
    snapshot: dict                # the boot volume's files (rekey.snapshot)
    data_key: bytes
    table: str
    vg: str
    lvs: list[LV]
    block_size: int
    fido2: bool                   # opened through a security key
    # The old pool's layout, read once: while the new disk's VG (of the same
    # name) is active, LVM won't read the old one.
    pool: thinpool.Pool | None = None

    @property
    def thin(self) -> list[LV]:
        return [lv for lv in self.lvs if lv.segtype == "thin"]

    @property
    def mapped(self) -> int:
        return sum(lv.mapped for lv in self.thin)

    def swap(self) -> LV | None:
        return next((lv for lv in self.lvs if lv.name == "swap"), None)


def blkid(dev: str, tag: str) -> str:
    return subprocess.run(["blkid", "-o", "value", "-s", tag, dev], stdout=subprocess.PIPE,
                          text=True).stdout.strip()


@contextlib.contextmanager
def old_region(cfg: Config, side: OldSide):
    with setup.data_region(cfg.disk, side.table, side.data_key, readonly=True, name=OLD_MAP) as dev:
        yield str(dev)


@contextlib.contextmanager
def old_thin_devices(side: OldSide, pv: str):
    """Read-only views of the old side's LVs (thin ones from thin_dump, like
    the boot environment's), and each thin LV's mapped runs.  Uses only the
    layout read by read_side: no LVM commands."""
    with contextlib.ExitStack() as stack:
        devs = stack.enter_context(thinpool.mapped(side.pool, writable=False))
        out = {}
        for lv in side.lvs:
            if lv.segtype == "thin":
                xml = cmd_output("thin_dump", "--dev-id", lv.thin_id, devs["tmeta"])
                table = thin_table(xml, devs["tdata"], lv.size // L.SECTOR)
                runs = thinpool.mappings(xml).get(int(lv.thin_id), [])
                dev = stack.enter_context(dm_device(f"{OLD_MAP}-{lv.name}", table, readonly=True))
                out[lv.name] = (str(dev), runs)
            elif lv.segtype == "linear":
                dev = stack.enter_context(dm_device(
                    f"{OLD_MAP}-{lv.name}", "".join(f"{s.start} {s.size} linear {pv} {s.pv_start}\n"
                                                    for s in lv.segments), readonly=True))
                out[lv.name] = (str(dev), [])
        yield out


def read_side(cfg: Config, offset: int, password: bytes) -> OldSide:
    found = setup.find_boot_key(cfg, offset, password)
    if found is None:
        raise RuntimeError("that password doesn't open a boot volume at that offset")
    key, slot = found
    with setup.boot_volume(cfg.disk, offset, key, create=False) as mnt:
        snap = rekey.snapshot(mnt)
    data_key, table = snap["data.key"][2], snap["table.txt"][2].decode()
    env = next((v[2].decode() for k, v in snap.items() if k.endswith("/env.sh")), "")
    vg = next((line.split("=", 1)[1].strip("'\"") for line in env.splitlines() if line.startswith("VG=")),
              cfg.vg)
    side = OldSide(offset=offset, password=password, snapshot=snap, data_key=data_key, table=table,
                   vg=vg, lvs=[], block_size=0, fido2=slot is not None)
    with old_region(cfg, side) as pv:
        for row in thinpool.lvm_out(pv, "lvs", "--noheadings", "--units", "b", "--nosuffix",
                                    "-o", "lv_name,segtype,lv_size", vg).splitlines():
            name, segtype, size = row.split()
            if segtype not in ("thin", "linear", "thin-pool"):
                raise RuntimeError(f"can't move LV {name} ({segtype})")
            if segtype != "thin-pool":
                side.lvs.append(LV(name, segtype, int(size)))
        if any(lv.segtype == "linear" and lv.name != "swap" for lv in side.lvs):
            raise RuntimeError("only thin LVs and the swap LV can be moved")
        pool = side.pool = thinpool.find_pool(vg, pv)
        for lv in side.lvs:
            if lv.segtype == "thin":
                lv.thin_id = thinpool.lvm_out(pv, "lvs", "--noheadings", "-o", "thin_id",
                                              f"{vg}/{lv.name}").strip()
            else:
                lv.segments = thinpool.lv_segments(vg, lv.name, pv, pool.pe_start)
        with thinpool.mapped(pool, writable=False) as devs:
            side.block_size = thinpool.usage(thinpool.dump(devs["tmeta"])).block_size
        with old_thin_devices(side, pv) as devs:
            for lv in side.lvs:
                dev, runs = devs[lv.name]
                lv.mapped = sum(n for _, _, n in runs) * side.block_size
                if lv.name == "swap":
                    lv.uuid = blkid(dev, "UUID")
                else:
                    lv.fs = blkid(dev, "TYPE")
    return side


def copy_runs(src: str, dst: str, runs, block_size: int) -> None:
    fs = os.open(src, os.O_RDONLY)
    fd = os.open(dst, os.O_WRONLY)
    try:
        for origin, _, n in runs:
            off, left = origin * block_size, n * block_size
            while left:
                chunk = min(left, COPY_BUFFER)
                buf = os.pread(fs, chunk, off)
                if len(buf) != chunk:
                    raise RuntimeError(f"short read from {src}")
                os.pwrite(fd, buf, off)
                off += chunk
                left -= chunk
        os.fsync(fd)
    finally:
        os.close(fs)
        os.close(fd)


def grow_fs(dev: str, fs: str) -> None:
    with mountpoint("grow-") as mnt:
        cmd("mount", "-t", fs, "-o", "nodiscard", dev, mnt)
        try:
            if fs == "btrfs":
                cmd("btrfs", "filesystem", "resize", "max", mnt, stdout=subprocess.DEVNULL)
            else:
                cmd("xfs_growfs", mnt, stdout=subprocess.DEVNULL)
        finally:
            cmd("umount", mnt)


def shrink_btrfs(dev: str, size: int) -> None:
    with mountpoint("shrink-") as mnt:
        cmd("mount", "-t", "btrfs", "-o", "nodiscard", dev, mnt)
        try:
            cmd("btrfs", "filesystem", "resize", str(size), mnt, stdout=subprocess.DEVNULL)
        finally:
            cmd("umount", mnt)


def build_side(cfg: Config, old_cfg: Config, dev: str, side: OldSide, *, hidden: bool, d: int,
               report) -> None:
    """LVM on the new data region `dev` (cfg: the new disk's config), then the
    old side's LVs (on old_cfg's disk) copied in."""
    vg = side.vg
    setup.lvm("pvcreate", dev, stdout=subprocess.DEVNULL)
    setup.lvm("vgcreate", vg, dev, stdout=subprocess.DEVNULL)
    swap = side.swap()
    if swap:
        setup.lvm("lvcreate", "-L", setup.lvm_size(swap.size), "-n", "swap", vg, stdout=subprocess.DEVNULL)
    if hidden:
        setup.lvm("lvcreate", "--type", "thin-pool", "-l", "1", "--discards", "nopassdown",
                  "-n", "thinpool", vg, stdout=subprocess.DEVNULL)
        setup.lvm("lvextend", "-l", "+100%FREE", f"{vg}/thinpool", stdout=subprocess.DEVNULL)
    else:
        setup.lvm("lvcreate", "--type", "thin-pool", "-l", "100%FREE", "--discards", "nopassdown",
                  "-n", "thinpool", vg, stdout=subprocess.DEVNULL)
    pool = int(setup.lvm_output("lvs", "--noheadings", "--units", "b", "--nosuffix", "-o", "lv_size",
                                f"{vg}/thinpool"))
    if side.mapped > pool * 0.95:
        raise RuntimeError(f"the side's data ({side.mapped // L.MIB} MiB) doesn't fit the new pool "
                           f"({pool // L.MIB} MiB)")
    if hidden:
        extent = int(setup.lvm_output("vgs", "--noheadings", "--units", "b", "--nosuffix",
                                      "-o", "vg_extent_size", vg))
        usual = min(pool, L.align_down(int(d * cfg.hidden_cap) // max(1, len(side.thin)), extent))
    else:
        usual = pool
    for lv in side.thin:
        size = max(lv.size, usual)
        setup.lvm("lvcreate", "-V", setup.lvm_size(size), "-T", f"{vg}/thinpool", "-n", lv.name,
                  stdout=subprocess.DEVNULL)
    with setup.vg_active(vg):
        if swap:
            cmd("mkswap", "-q", *(["-U", swap.uuid] if swap.uuid else []), f"/dev/{vg}/swap")
        # The pool zeroes new chunks (LVM's default) while copying, so blocks
        # the old pool read as zeros still do; the usual -Zn afterwards.
        with old_region(old_cfg, side) as pv, old_thin_devices(side, pv) as devs:
            for lv in side.thin:
                src, runs = devs[lv.name]
                report(f"copying {lv.name}: {lv.mapped // L.MIB} MiB")
                copy_runs(src, f"/dev/{vg}/{lv.name}", runs, side.block_size)
        setup.lvm("lvchange", "-Zn", f"{vg}/thinpool", stdout=subprocess.DEVNULL)
        for lv in side.thin:
            dev_lv = f"/dev/{vg}/{lv.name}"
            if usual > lv.size and lv.fs in ("btrfs", "xfs"):
                report(f"growing {lv.name}'s {lv.fs} filesystem")
                grow_fs(dev_lv, lv.fs)
            elif lv.size > usual and lv.fs == "btrfs":
                # On a smaller disk: back to the usual size, as an installer
                # would have made it.
                report(f"shrinking {lv.name}'s btrfs filesystem to {usual // L.MIB} MiB")
                shrink_btrfs(dev_lv, usual)
                # The filesystem is already smaller; LVM can't check btrfs itself.
                setup.lvm("lvreduce", "-f", "--fs", "ignore", "-L", setup.lvm_size(usual), f"{vg}/{lv.name}",
                          stdout=subprocess.DEVNULL)
            elif lv.size > usual:
                report(f"note: {lv.name} keeps its size ({lv.size // L.MIB} MiB, more than the usual "
                       f"{usual // L.MIB} MiB): {lv.fs or 'its filesystem'} can't shrink")


def boot_files(side: OldSide) -> dict:
    """The old boot volume's systems/ (env.sh and extra.sh), for
    write_boot_volume; everything else is regenerated."""
    systems = {}
    for rel, (kind, _, data) in side.snapshot.items():
        parts = rel.split("/")
        if len(parts) == 3 and parts[0] == "systems" and kind == "file":
            systems.setdefault(parts[1], {})[parts[2]] = data
    return {name: files for name, files in systems.items() if "env.sh" in files}


def default_system(side: OldSide) -> str | None:
    entry = side.snapshot.get("systems/default")
    return entry[2].decode() if entry and entry[0] == "link" else None


def move(cfg: Config, new_disk: str, decoy_password: bytes,
         hidden: tuple[int, bytes] | None, *, randomize: bool = True, report=print) -> L.HiddenLayout | None:
    """Move the decoy (and the hidden side at `hidden` = (offset in bytes,
    password), if any) from cfg.disk to new_disk.  Returns the new hidden
    layout."""
    if os.path.realpath(new_disk) == os.path.realpath(cfg.disk):
        raise RuntimeError("the new disk is the old disk")
    new_cfg = dataclasses.replace(cfg, disk=new_disk)
    d = setup.check_disk(new_disk)
    report("reading the old decoy")
    decoy = read_side(cfg, L.DECOY_PLAIN_OFFSET, decoy_password)
    old_hidden = None
    if hidden:
        report("reading the old hidden side")
        old_hidden = read_side(cfg, hidden[0], hidden[1])
    # A first check, before anything is written: the data has to fit.
    room = L.data_end(d) - L.DATA_START
    if decoy.mapped + (old_hidden.mapped if old_hidden else 0) > room * 0.9:
        raise RuntimeError("the data doesn't fit on the new disk")
    if old_hidden and old_hidden.mapped > d * cfg.hidden_cap:
        raise RuntimeError(f"the hidden side's data ({old_hidden.mapped // L.MIB} MiB) exceeds its cap "
                           f"on the new disk ({int(d * cfg.hidden_cap) // L.MIB} MiB)")

    try:
        if randomize:
            report("randomizing the new disk")
            setup.randomize(new_disk)
        else:
            header.write_edges(new_disk)
        # The new disk has its own salt (unless the config sets one).
        salt = header.salt(new_cfg)

        report("creating the decoy on the new disk")
        key = argon2(decoy_password, salt, cfg.argon2)
        decoy_key = os.urandom(64)
        decoy_table = L.linear_table(L.DATA_START, L.data_end(d))
        with setup.boot_volume(new_disk, L.DECOY_PLAIN_OFFSET, key, create=True) as mnt:
            setup.write_boot_volume(mnt, new_cfg, decoy_key, decoy_table,
                                    systems_from=boot_files(decoy), default=default_system(decoy))
        with setup.data_region(new_disk, decoy_table, decoy_key, readonly=False) as dev:
            setup.LVM_DEVICES = str(dev)
            build_side(new_cfg, cfg, str(dev), decoy, hidden=False, d=d, report=report)
            segs = setup.lvm_segments(decoy.vg)
            pe_start = setup.pv_data_offset(str(dev))
            setup.LVM_DEVICES = None
        if not old_hidden:
            return None

        report("creating the hidden side on the new disk")
        top = setup.decoy_top(segs, pe_start, L.DATA_START, d)
        hl = L.hidden_layout(top, memorable=cfg.memorable_offset)
        setup.check_no_overlap(segs, pe_start, hl)
        hkey = argon2(old_hidden.password, salt, cfg.argon2)
        hidden_key = os.urandom(64)
        frontier = old_hidden.snapshot.get("frontier.env")
        extra_env = b"".join(line + b"\n" for line in (frontier[2] if frontier else b"").splitlines()
                             if not line.startswith((b"MIN_GAP_PERCENT=", b"FLOOR_SECTOR="))).decode()
        extra_env += f"FLOOR_SECTOR={setup.floor_below(new_cfg, hl, d) // L.SECTOR}\n"
        with setup.boot_volume(new_disk, hl.plain_offset, hkey, create=True) as mnt:
            setup.write_boot_volume(mnt, new_cfg, hidden_key, hl.data_table(),
                                    systems_from=boot_files(old_hidden),
                                    default=default_system(old_hidden))
            setup.install_hidden_extras(mnt, new_cfg, decoy_key, decoy_table, extra_env)
        with setup.data_region(new_disk, hl.data_table(), hidden_key, readonly=False) as dev:
            setup.LVM_DEVICES = str(dev)
            build_side(new_cfg, cfg, str(dev), old_hidden, hidden=True, d=d, report=report)
        return hl
    finally:
        setup.LVM_DEVICES = None

