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
  spare is filled with random data, so a stale copy is never mistaken for a
  good one later.

Traces (docs/design.md §10.5): moving data out of the top of the pool isn't
enough for a hidden side to go there.  The pool's metadata is copy-on-write,
so its free blocks keep old btree nodes saying where the decoy's data used to
be, and the blocks the decoy freed keep its old data (no discard ever reaches
the disk).  If hidden data later overwrote them, an examiner with the decoy
password could see decoy data turn into noise.  So `clean` rewrites the
metadata over random data, and refills every free pool block above the
boundary with random data written straight to the disk (through a throwaway
key), which is what never-used space looks like.

Destinations are always below the side's own highest allocated block, so the
other side's data (which, without a collision, lies entirely beyond it) is
never touched.  The refill above the boundary stops below the hidden side's
lowest block when one exists.
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
from pd.util import cmd

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
            # At /mnt, where anyone would mount it by hand: ext4 records
            # the last mount point in its superblock.
            mnt = pathlib.Path("/mnt")
            if os.path.ismount(mnt):
                raise RuntimeError("/mnt is in use; unmount it first")
            mnt.mkdir(exist_ok=True)
            cmd("mount", "-t", fs, "-o", "nodiscard", dev, mnt)
            try:
                cmd("fstrim", mnt)
            finally:
                cmd("umount", mnt)


def fill_random(dev: str) -> None:
    """Overwrite all of dev with random bytes (dev is a decrypted view, so
    the disk gets random-looking ciphertext)."""
    size = setup.device_size(dev)
    fd = os.open(dev, os.O_WRONLY)
    try:
        pos = 0
        while pos < size:
            n = min(4 * MIB, size - pos)
            os.pwrite(fd, os.urandom(n), pos)
            pos += n
        os.fsync(fd)
    finally:
        os.close(fd)


def write_metadata(devs: dict, xml_path: pathlib.Path, xml: str, report) -> None:
    """Replace the pool's metadata with `xml`, over random data, so no stale
    btree nodes survive in its free blocks.  Staged in the spare first."""
    if "spare" in devs:
        cmd("thin_restore", "-q", "-i", xml_path, "-o", devs["spare"])
        if not thinpool.check(devs["spare"]):
            raise RuntimeError("the new metadata doesn't pass thin_check; the pool is unchanged")
    else:
        report("warning: no spare metadata LV; an interruption from here on loses the pool")
    fill_random(devs["tmeta"])
    cmd("thin_restore", "-q", "-i", xml_path, "-o", devs["tmeta"])
    if not thinpool.check(devs["tmeta"]):
        raise RuntimeError("writing the new metadata failed; run `pd compact --recover`")
    if thinpool.mappings(thinpool.dump(devs["tmeta"])) != thinpool.mappings(xml):
        raise RuntimeError("the written metadata differs; run `pd compact --recover`")
    if "spare" in devs:
        fill_random(devs["spare"])


def wipe_above(pool: thinpool.Pool, region_table: str, nr_blocks: int, block_size: int,
               base: str, base_offset: int, limit: int | None, report) -> None:
    """Refill the pool's data blocks from `nr_blocks` on (none of which may be
    mapped) with random data, written to the disk through a throwaway key.
    `base` is the device the region table's offsets are relative to, minus
    `base_offset`; `limit` is a disk byte to stop below (the hidden side)."""
    ranges = []
    for s in pool.tdata:
        lo = max(s.start * L.SECTOR, nr_blocks * block_size)
        hi = (s.start + s.size) * L.SECTOR
        if lo >= hi:
            continue
        p_lo = L.logical_to_physical(region_table, pool.tdata_to_pv(lo))
        p_hi = L.logical_to_physical(region_table, pool.tdata_to_pv(hi - 1)) + 1
        if limit is not None:
            p_hi = min(p_hi, limit)
        if p_hi > p_lo:
            ranges.append((p_lo, p_hi))
    total = sum(hi - lo for lo, hi in ranges)
    if not total:
        return
    report(f"refilling {total // MIB} MiB of free space above the boundary with random data")
    table, pos = "", 0
    for lo, hi in ranges:
        n = (hi - lo) // L.SECTOR
        table += f"{pos} {n} linear {base} {(lo - base_offset) // L.SECTOR}\n"
        pos += n
    with setup.dm_device("pd-wipe-raw", table) as raw:
        cmd("cryptsetup", "open", "--type=plain", "--batch-mode", *setup.CIPHER_ARGS,
            "--key-file=/dev/urandom", raw, "pd-wipe")
        try:
            setup.fill_zeros("/dev/mapper/pd-wipe")
        finally:
            cmd("cryptsetup", "close", "pd-wipe")


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
        fill_random(devs["spare"])
    return "restored the pool's metadata from the spare"


def shrink_metadata(pool: thinpool.Pool, nr_blocks: int, report) -> thinpool.Usage:
    """Move data above block `nr_blocks` below it (if any is there) and
    rewrite the metadata over random data.  Returns the usage afterwards."""
    with thinpool.mapped(pool, writable=True) as devs, scratch() as tmp:
        if not thinpool.check(devs["tmeta"]):
            raise RuntimeError("the pool's metadata doesn't pass thin_check; if a compaction was "
                               "interrupted, run `pd compact --recover`")
        old_xml = thinpool.dump(devs["tmeta"])
        before = thinpool.usage(old_xml)
        (tmp / "old.xml").write_text(old_xml)
        if before.high_water > nr_blocks:
            report(f"moving data from above block {nr_blocks} (of {before.nr_blocks}, "
                   f"{before.block_size // 1024} KiB each); this copies up to "
                   f"{(before.high_water - nr_blocks) * before.block_size // MIB} MiB")
            cmd("pdata_tools", "thin_shrink", "--input", tmp / "old.xml", "--output", tmp / "new.xml",
                "--data", devs["tdata"], "--nr-blocks", str(nr_blocks))
            # thin_shrink also shrinks the pool to nr_blocks; keep its size.
            new_xml = (tmp / "new.xml").read_text()
            new_xml = re.sub(r'(<superblock [^>]*nr_data_blocks=")\d+(")',
                             rf"\g<1>{before.nr_blocks}\g<2>", new_xml, count=1)
        else:
            new_xml = old_xml
        after = thinpool.usage(new_xml)
        old_maps, new_maps = thinpool.mappings(old_xml), thinpool.mappings(new_xml)
        same_shape = old_maps.keys() == new_maps.keys() and all(
            sum(n for _, _, n in old_maps[k]) == sum(n for _, _, n in new_maps[k]) for k in old_maps)
        if after.mapped != before.mapped or after.high_water > nr_blocks or not same_shape:
            raise RuntimeError("thin_shrink's result doesn't add up; the pool is unchanged")
        (tmp / "new.xml").write_text(new_xml)
        report("rewriting the pool's metadata")
        write_metadata(devs, tmp / "new.xml", new_xml, report)
    return after


def compact_pool(cfg: Config, pool: thinpool.Pool, region_table: str, target, *,
                 do_trim: bool, wipe: tuple[str, int, int | None] | None,
                 report=print) -> tuple[thinpool.Usage, thinpool.Usage, int]:
    """target(pool, usage) -> the number of leading blocks to keep data in.
    wipe: (base device, base offset, limit) for wipe_above, or None (the
    hidden side: its free space holds nothing readable with the decoy key).
    Returns (usage before, usage after, boundary)."""
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
    after = shrink_metadata(pool, nr_blocks, report)
    if wipe:
        wipe_above(pool, region_table, nr_blocks, before.block_size, *wipe, report=report)
    # Activating the pool runs LVM's own thin_check.
    try:
        with setup.vg_active(pool.vg):
            pass
    finally:
        setup.lvm("vgchange", "-an", pool.vg, check=False)
    return before, after, nr_blocks


def hidden_low(cfg: Config, offset: int, password: bytes) -> int:
    """The lowest disk byte the hidden side at `offset` has allocated (the
    start of the 256 MiB chunk holding its pool's highest block)."""
    key = setup.boot_key(cfg, offset, password, "the hidden password")
    with setup.boot_volume(cfg.disk, offset, key, create=False) as mnt:
        data_key = (mnt / "data.key").read_bytes()
        table = (mnt / "table.txt").read_text()
    return region_low(cfg, table, data_key)


def region_low(cfg: Config, table: str, data_key: bytes) -> int:
    """hidden_low for a hidden data region given by its table and key."""
    with setup.data_region(cfg.disk, table, data_key, readonly=True, name="pd-hidden") as dev:
        pool = thinpool.find_pool(cfg.vg, str(dev))
        with thinpool.mapped(pool, writable=False) as devs:
            u = thinpool.usage(thinpool.dump(devs["tmeta"]))
        top = pool.tdata_to_pv(max(u.high_water, 1) * u.block_size - 1)
        chunk = top // L.REVERSE_CHUNK * L.REVERSE_CHUNK
        return L.logical_to_physical(table.replace("DISK", cfg.disk), chunk)


def compact(cfg: Config, offset: int, password: bytes, *, below: int | None = None,
            do_trim: bool = True, hidden: tuple[int, bytes] | str | None = None,
            report=print) -> tuple[thinpool.Usage, thinpool.Usage, int]:
    """Compact the pd side whose boot volume is at `offset`.  `below`: keep
    its data within the first `below` bytes of its pool's data; by default,
    for the decoy, low enough for a hidden side at its cap (what `pd
    init-hidden` checks), and for the hidden side as tightly as possible.
    For the decoy, `hidden` says whether a hidden side exists ("none", or
    (offset, password)), which bounds the refill above the boundary.
    Returns (usage before, usage after, boundary in blocks)."""
    decoy = offset == L.DECOY_PLAIN_OFFSET
    limit = None
    if decoy:
        if hidden is None:
            raise RuntimeError("compacting the decoy needs --no-hidden or --hidden-offset: the free "
                               "space above the boundary is refilled with random data, which must "
                               "stop below a hidden side")
        if hidden != "none":
            limit = hidden_low(cfg, hidden[0], hidden[1]) - L.REVERSE_CHUNK
    key = setup.boot_key(cfg, offset, password)
    with setup.boot_volume(cfg.disk, offset, key, create=False) as mnt:
        data_key = (mnt / "data.key").read_bytes()
        table = (mnt / "table.txt").read_text()
    d = setup.check_disk(cfg.disk)
    with setup.data_region(cfg.disk, table, data_key, readonly=False) as dev:
        pool = thinpool.find_pool(cfg.vg, str(dev))
        region = table.replace("DISK", cfg.disk)
        segs = setup.lvm_segments(cfg.vg)

        def target(pool, u):
            if below is not None:
                return below // u.block_size
            if decoy:
                floor = setup.hidden_floor(cfg, segs, pool.pe_start * L.SECTOR,
                                           L.table_physical_range(table)[0], d)
                return thinpool.blocks_below(pool, region, floor, u.block_size)
            return u.mapped

        return compact_pool(cfg, pool, region, target, do_trim=do_trim,
                            wipe=(cfg.disk, 0, limit) if decoy else None, report=report)


def clean_decoy(cfg: Config, password: bytes, floor: int, report=print) -> None:
    """Before init-hidden puts a hidden side above `floor` on a decoy that
    has been used: rewrite its pool metadata and refill the space above the
    floor (see the module docstring).  The decoy's data must already be
    below the floor."""
    def target(pool, u):
        return thinpool.blocks_below(pool, region, floor, u.block_size)
    key = setup.boot_key(cfg, L.DECOY_PLAIN_OFFSET, password, "the decoy password")
    with setup.boot_volume(cfg.disk, L.DECOY_PLAIN_OFFSET, key, create=False) as mnt:
        data_key = (mnt / "data.key").read_bytes()
        table = (mnt / "table.txt").read_text()
    with setup.data_region(cfg.disk, table, data_key, readonly=False) as dev:
        pool = thinpool.find_pool(cfg.vg, str(dev))
        region = table.replace("DISK", cfg.disk)
        compact_pool(cfg, pool, region, target, do_trim=False, wipe=(cfg.disk, 0, None), report=report)


def compact_stock(cfg: Config, device: str, header: str | None, password: bytes, *,
                  below: int | None = None, do_trim: bool = True, hidden_limit: int | None = None,
                  floor: int | None = None, report=print) -> tuple[thinpool.Usage, thinpool.Usage, int]:
    """Compact a stock decoy (LUKS + LVM thin, docs/stock-boot.md) so a hidden
    side fits at the top of its pool data, and refill the space above the
    boundary (stopping below `hidden_limit` if a hidden side exists).
    `floor`: use this disk byte as the boundary."""
    d = setup.check_disk(cfg.disk)
    start = setup.partition_offset(device, cfg.disk)
    with setup.stock_decoy_opened(device, password, header, readonly=False) as dev:
        _, offset, length, _ = setup.crypt_plain_args(
            setup.cmd_output("dmsetup", "table", "--showkeys", setup.STOCK_MAP))
        vg = setup.lvm_output("pvs", "--noheadings", "-o", "vg_name", dev)
        pool = thinpool.find_pool(vg, str(dev))
        pv_start = start + offset * L.SECTOR
        region = L.linear_table(pv_start, pv_start + length * L.SECTOR, dev=cfg.disk)

        def target(pool, u):
            if below is not None:
                return below // u.block_size
            f = floor if floor is not None else setup.stock_floor(cfg, pool, region, d)
            return thinpool.blocks_below(pool, region, f, u.block_size)

        # The refill goes through the LUKS partition: while it's open, the
        # kernel refuses mappings of the whole disk.
        return compact_pool(cfg, pool, region, target, do_trim=do_trim,
                            wipe=(os.path.realpath(device), start, hidden_limit), report=report)


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
