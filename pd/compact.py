"""Compact a side's thin pool in place (`pd compact`, docs/design.md §10.4).

A thin pool's allocations spread over its whole data area once it has been
full, even after the data is deleted and trimmed.  A hidden side needs the
top of the decoy's pool data to be free, so this moves every allocated data
block above a boundary into free blocks below it, and rewrites the pool's
metadata to point at the new locations.  The pool stays inactive throughout;
its metadata and data are mapped directly.  thin_shrink (from
thin-provisioning-tools) does the moving.

Crash safety:
* Data is only ever copied into blocks no thin device uses, so until the
  metadata is replaced, the pool is unchanged.
* The new metadata is written to LVM's spare metadata LV (pmspare) first and
  checked, then to the pool's metadata.  If writing the latter is
  interrupted, `pd compact --recover` copies the spare back.  Afterwards the
  spare's superblock is wiped, so a stale copy is never mistaken for a good
  one later.

Destinations are always below the side's own highest allocated block, so the
other side's data (which, without a collision, lies entirely beyond it) is
never touched.
"""

import contextlib
import os
import pathlib
import re
import subprocess
import tempfile

from pd import layout as L
from pd import setup, thinpool
from pd.config import Config
from pd.util import cmd, mountpoint

MIB = L.MIB


@contextlib.contextmanager
def scratch():
    """A private directory in RAM (/run) for the metadata dumps."""
    path = pathlib.Path(tempfile.mkdtemp(prefix="pd-compact-", dir="/run"))
    os.chmod(path, 0o700)
    try:
        yield path
    finally:
        for f in path.iterdir():
            f.unlink()
        path.rmdir()


def trim(vg: str, pv: str) -> None:
    """Mount every thin LV's filesystem and fstrim it, so the pool unmaps
    what the filesystems have freed.  (The pool is nopassdown and dm-crypt
    refuses discards: nothing reaches the disk.)"""
    rows = [r.split() for r in thinpool.lvm_out(pv, "lvs", "--noheadings", "-o", "lv_name,segtype", vg)
            .splitlines()]
    with setup.vg_active(vg):
        for name, segtype in rows:
            if segtype != "thin":
                continue
            dev = f"/dev/{vg}/{name}"
            fs = subprocess.run(["blkid", "-o", "value", "-s", "TYPE", dev], stdout=subprocess.PIPE,
                                text=True).stdout.strip()
            if fs not in ("btrfs", "xfs", "ext4", "ext3", "ext2", "f2fs"):
                print(f"pd: not trimming {name} (filesystem: {fs or 'none'})")
                continue
            with mountpoint("trim-") as mnt:
                cmd("mount", "-t", fs, "-o", "nodiscard", dev, mnt)
                try:
                    cmd("fstrim", mnt)
                finally:
                    cmd("umount", mnt)


def wipe_superblock(dev: str) -> None:
    with open(dev, "r+b") as f:
        f.write(bytes(4096))
        f.flush()
        os.fsync(f.fileno())


def copy_device(src: str, dst: str) -> None:
    cmd("dd", f"if={src}", f"of={dst}", "bs=4M", "conv=fsync", "status=none")


def recover(pool: thinpool.Pool) -> str:
    """After an interrupted compaction: if the pool's metadata is damaged
    and the spare holds a good copy, restore it."""
    with thinpool.mapped(pool, writable=True) as devs:
        if thinpool.check(devs["tmeta"]):
            return "the pool's metadata is fine; nothing to recover"
        if "spare" not in devs or not thinpool.check(devs["spare"]):
            raise RuntimeError("the pool's metadata is damaged and the spare has no good copy; "
                               "try `lvconvert --repair`")
        copy_device(devs["spare"], devs["tmeta"])
        if not thinpool.check(devs["tmeta"]):
            raise RuntimeError("restoring the metadata from the spare failed")
        wipe_superblock(devs["spare"])
    return "restored the pool's metadata from the spare"


def shrink_metadata(pool: thinpool.Pool, nr_blocks: int, report) -> thinpool.Usage:
    """Move data above block `nr_blocks` below it and write the new metadata.
    Returns the usage afterwards."""
    with thinpool.mapped(pool, writable=True) as devs, scratch() as tmp:
        if not thinpool.check(devs["tmeta"]):
            raise RuntimeError("the pool's metadata doesn't pass thin_check; if a compaction was "
                               "interrupted, run `pd compact --recover`")
        old_xml = thinpool.dump(devs["tmeta"])
        before = thinpool.usage(old_xml)
        (tmp / "old.xml").write_text(old_xml)
        report(f"moving data from above block {nr_blocks} (of {before.nr_blocks}, "
               f"{before.block_size // 1024} KiB each); this copies up to "
               f"{max(0, before.high_water - nr_blocks) * before.block_size // MIB} MiB")
        cmd("pdata_tools", "thin_shrink", "--input", tmp / "old.xml", "--output", tmp / "new.xml",
            "--data", devs["tdata"], "--nr-blocks", str(nr_blocks))
        # thin_shrink also shrinks the pool to nr_blocks; keep its size.
        new_xml = (tmp / "new.xml").read_text()
        new_xml = re.sub(r'(<superblock [^>]*nr_data_blocks=")\d+(")',
                         rf"\g<1>{before.nr_blocks}\g<2>", new_xml, count=1)
        after = thinpool.usage(new_xml)
        old_maps, new_maps = thinpool.mappings(old_xml), thinpool.mappings(new_xml)
        same_shape = old_maps.keys() == new_maps.keys() and all(
            sum(n for _, _, n in old_maps[k]) == sum(n for _, _, n in new_maps[k]) for k in old_maps)
        if after.mapped != before.mapped or after.high_water > nr_blocks or not same_shape:
            raise RuntimeError("thin_shrink's result doesn't add up; the pool is unchanged")
        (tmp / "new.xml").write_text(new_xml)
        if "spare" in devs:
            cmd("thin_restore", "-q", "-i", tmp / "new.xml", "-o", devs["spare"])
            if not thinpool.check(devs["spare"]):
                raise RuntimeError("the new metadata doesn't pass thin_check; the pool is unchanged")
        else:
            report("warning: no spare metadata LV; an interruption from here on loses the pool")
        cmd("thin_restore", "-q", "-i", tmp / "new.xml", "-o", devs["tmeta"])
        if not thinpool.check(devs["tmeta"]):
            raise RuntimeError("writing the new metadata failed; run `pd compact --recover`")
        if thinpool.mappings(thinpool.dump(devs["tmeta"])) != thinpool.mappings(new_xml):
            raise RuntimeError("the written metadata differs; run `pd compact --recover`")
        if "spare" in devs:
            wipe_superblock(devs["spare"])
    return after


def compact_pool(cfg: Config, pool: thinpool.Pool, region_table: str, target, *,
                 do_trim: bool, report=print) -> tuple[thinpool.Usage, thinpool.Usage]:
    """target(pool, usage) -> the number of leading blocks to keep data in."""
    # Check the metadata before anything activates the pool (activation
    # fails on damaged metadata and can leave mappings behind).
    with thinpool.mapped(pool, writable=False) as devs:
        if not thinpool.check(devs["tmeta"]):
            raise RuntimeError("the pool's metadata doesn't pass thin_check; if a compaction was "
                               "interrupted, run `pd compact --recover`")
    try:
        if do_trim:
            report("trimming the filesystems")
            trim(pool.vg, pool.pv)
    finally:
        setup.lvm("vgchange", "-an", pool.vg, check=False)
    with thinpool.mapped(pool, writable=False) as devs:
        before = thinpool.usage(thinpool.dump(devs["tmeta"]))
    nr_blocks = target(pool, before)
    if nr_blocks < before.mapped:
        raise RuntimeError(f"the side's data ({before.mapped * before.block_size // MIB} MiB) doesn't "
                           f"fit below the boundary ({nr_blocks * before.block_size // MIB} MiB)")
    if before.high_water <= nr_blocks:
        report("nothing to move: all data is already below the boundary")
        return before, before
    after = shrink_metadata(pool, nr_blocks, report)
    # Activating the pool runs LVM's own thin_check.
    try:
        with setup.vg_active(pool.vg):
            pass
    finally:
        setup.lvm("vgchange", "-an", pool.vg, check=False)
    return before, after


def compact(cfg: Config, offset: int, password: bytes, *, below: int | None = None,
            do_trim: bool = True, report=print) -> tuple[thinpool.Usage, thinpool.Usage, int]:
    """Compact the pd side whose boot volume is at `offset`.  `below`: keep
    its data within the first `below` bytes of its pool's data; by default,
    for the decoy, low enough for a hidden side at its cap (what `pd
    init-hidden` checks), and for the hidden side as tightly as possible.
    Returns (usage before, usage after, boundary in blocks)."""
    key = setup.boot_key(cfg, offset, password)
    with setup.boot_volume(cfg.disk, offset, key, create=False) as mnt:
        data_key = (mnt / "data.key").read_bytes()
        table = (mnt / "table.txt").read_text()
    d = setup.check_disk(cfg.disk)
    decoy = offset == L.DECOY_PLAIN_OFFSET
    with setup.data_region(cfg.disk, table, data_key, readonly=False) as dev:
        pool = thinpool.find_pool(cfg.vg, str(dev))
        region = table.replace("DISK", cfg.disk)
        boundary = [0]
        segs = setup.lvm_segments(cfg.vg)

        def target(pool, u):
            if below is not None:
                n = below // u.block_size
            elif decoy:
                floor = setup.hidden_floor(cfg, segs, pool.pe_start * L.SECTOR,
                                           L.table_physical_range(table)[0], d)
                n = thinpool.blocks_below(pool, region, floor, u.block_size)
            else:
                n = u.mapped
            boundary[0] = n
            return n

        before, after = compact_pool(cfg, pool, region, target, do_trim=do_trim, report=report)
    return before, after, boundary[0]


def compact_stock(cfg: Config, device: str, header: str | None, password: bytes, *,
                  below: int | None = None, do_trim: bool = True,
                  report=print) -> tuple[thinpool.Usage, thinpool.Usage, int]:
    """Compact a stock decoy (LUKS + LVM thin, docs/stock-boot.md) so a hidden
    side fits at the top of its pool data."""
    d = setup.check_disk(cfg.disk)
    start = setup.partition_offset(device, cfg.disk)
    with setup.stock_decoy_opened(device, password, header, readonly=False) as dev:
        _, offset, length, _ = setup.crypt_plain_args(
            setup.cmd_output("dmsetup", "table", "--showkeys", setup.STOCK_MAP))
        vg = setup.lvm_output("pvs", "--noheadings", "-o", "vg_name", dev)
        pool = thinpool.find_pool(vg, str(dev))
        pv_start = start + offset * L.SECTOR
        region = L.linear_table(pv_start, pv_start + length * L.SECTOR, dev=cfg.disk)
        boundary = [0]

        def target(pool, u):
            if below is not None:
                n = below // u.block_size
            else:
                n = thinpool.blocks_below(pool, region, setup.stock_floor(cfg, pool, region, d),
                                          u.block_size)
            boundary[0] = n
            return n

        before, after = compact_pool(cfg, pool, region, target, do_trim=do_trim, report=report)
    return before, after, boundary[0]


def recover_side(cfg: Config, args, password: bytes) -> str:
    """`pd compact --recover`: open the side's data region and restore its pool
    metadata from the spare if it's damaged."""
    if args.stock_decoy:
        with setup.stock_decoy_opened(args.stock_decoy, password, args.decoy_header,
                                      readonly=False) as dev:
            vg = setup.lvm_output("pvs", "--noheadings", "-o", "vg_name", dev)
            return recover(thinpool.find_pool(vg, str(dev)))
    from pd import rekey
    offset = rekey.default_offset(cfg, args.offset)
    key = setup.boot_key(cfg, offset, password)
    with setup.boot_volume(cfg.disk, offset, key, create=False) as mnt:
        data_key = (mnt / "data.key").read_bytes()
        table = (mnt / "table.txt").read_text()
    with setup.data_region(cfg.disk, table, data_key, readonly=False) as dev:
        return recover(thinpool.find_pool(cfg.vg, str(dev)))
