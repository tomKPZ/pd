"""Thin pool internals: a pool's metadata and data devices mapped directly with
dm-linear (the pool itself stays inactive), and what its metadata says about
where data lives.  Used by `pd compact` and by `pd init-hidden`'s check that
the decoy leaves room for a hidden side."""

import contextlib
import dataclasses
import re
import subprocess

from pd import layout as L
from pd.util import cmd, cmd_output, dm_device

LVM_NO_ARCHIVE = "backup { backup = 0 archive = 0 }"


def lvm_out(pv: str, *args: str, readonly: bool = True) -> str:
    return cmd_output("lvm", *args, *(["--readonly"] if readonly else []), "--devices", pv,
                      "--config", LVM_NO_ARCHIVE, stderr=subprocess.DEVNULL)


@dataclasses.dataclass
class Segment:
    start: int      # LV-relative sector
    size: int       # sectors
    pv_start: int   # PV-relative sector


@dataclasses.dataclass
class Pool:
    vg: str
    name: str
    pv: str
    pe_start: int                    # sectors
    tmeta: list[Segment]
    tdata: list[Segment]
    spare: list[Segment] | None      # LVM's pool metadata spare (pmspare), if any

    def table(self, segs: list[Segment]) -> str:
        return "".join(f"{s.start} {s.size} linear {self.pv} {s.pv_start}\n" for s in segs)

    def tdata_to_pv(self, byte: int) -> int:
        """PV byte offset of a byte of the pool's data device."""
        sector = byte // L.SECTOR
        for s in self.tdata:
            if s.start <= sector < s.start + s.size:
                return (s.pv_start + sector - s.start) * L.SECTOR + byte % L.SECTOR
        raise ValueError(f"byte {byte} is outside the pool's data")


def lv_segments(vg: str, lv: str, pv: str, pe_start: int) -> list[Segment]:
    out = lvm_out(pv, "lvs", "-a", "--noheadings", "--nosuffix", "--units", "s",
                  "-o", "seg_start,seg_size,seg_pe_ranges,vg_extent_size", f"{vg}/{lv}")
    segs = []
    for row in out.splitlines():
        start, size, ranges, extent = row.split()
        first = int(ranges.rsplit(":", 1)[1].split("-")[0])
        segs.append(Segment(int(start), int(size), pe_start + first * int(extent)))
    return sorted(segs, key=lambda s: s.start)


def find_pool(vg: str, pv: str) -> Pool:
    pe_start = int(lvm_out(pv, "pvs", "--noheadings", "--nosuffix", "--units", "s", "-o", "pe_start", pv))
    rows = [r.split() for r in lvm_out(pv, "lvs", "-a", "--noheadings", "-o", "lv_name,segtype", vg).splitlines()]
    pools = [r[0] for r in rows if len(r) == 2 and r[1] == "thin-pool"]
    if len(pools) != 1:
        raise RuntimeError(f"volume group {vg} needs exactly one thin pool (found {len(pools)})")
    name = pools[0]
    spares = [r[0].strip("[]") for r in rows if r and r[0].strip("[]").endswith("_pmspare")]
    return Pool(vg=vg, name=name, pv=pv, pe_start=pe_start,
                tmeta=lv_segments(vg, f"{name}_tmeta", pv, pe_start),
                tdata=lv_segments(vg, f"{name}_tdata", pv, pe_start),
                spare=lv_segments(vg, spares[0], pv, pe_start) if spares else None)


@contextlib.contextmanager
def mapped(pool: Pool, *, writable: bool):
    """The pool's metadata, data and spare as dm-linear devices.  The pool
    (and its VG) must be inactive."""
    with contextlib.ExitStack() as stack:
        devs = {}
        parts = [("tmeta", pool.tmeta), ("tdata", pool.tdata)] + ([("spare", pool.spare)] if pool.spare else [])
        for part, segs in parts:
            devs[part] = str(stack.enter_context(
                dm_device(f"pd-pool-{part}", pool.table(segs), readonly=not writable)))
        yield devs


@dataclasses.dataclass
class Usage:
    block_size: int          # bytes
    nr_blocks: int
    mapped: int              # data blocks in use
    high_water: int          # blocks [0, high_water) contain every mapped block
    transaction: int


def usage(xml: str) -> Usage:
    sb = re.search(r"<superblock [^>]*>", xml).group(0)
    attr = lambda name: int(re.search(rf'{name}="(\d+)"', sb).group(1))  # noqa: E731
    used = top = 0
    for begin, length in re.findall(r'data_begin="(\d+)" length="(\d+)"', xml):
        used += int(length)
        top = max(top, int(begin) + int(length))
    for block in re.findall(r'data_block="(\d+)"', xml):
        used += 1
        top = max(top, int(block) + 1)
    return Usage(block_size=attr("data_block_size") * L.SECTOR, nr_blocks=attr("nr_data_blocks"),
                 mapped=used, high_water=top, transaction=attr("transaction"))


def mappings(xml: str) -> dict[int, list[tuple[int, int, int]]]:
    """Each thin device's mappings as merged (origin, data, length) runs, for
    comparing metadata independently of how it's written."""
    out: dict[int, list[tuple[int, int, int]]] = {}
    dev = None
    for m in re.finditer(r"<(device|range_mapping|single_mapping) ([^>]*)>", xml):
        attrs = dict(re.findall(r'(\w+)="(\d+)"', m.group(2)))
        if m.group(1) == "device":
            dev = int(attrs["dev_id"])
            out[dev] = []
            continue
        if m.group(1) == "range_mapping":
            o, d, n = int(attrs["origin_begin"]), int(attrs["data_begin"]), int(attrs["length"])
        else:
            o, d, n = int(attrs["origin_block"]), int(attrs["data_block"]), 1
        runs = out[dev]
        if runs and runs[-1][0] + runs[-1][2] == o and runs[-1][1] + runs[-1][2] == d:
            runs[-1] = (runs[-1][0], runs[-1][1], runs[-1][2] + n)
        else:
            runs.append((o, d, n))
    return out


def dump(tmeta: str) -> str:
    return cmd_output("thin_dump", tmeta)


def check(dev: str) -> bool:
    return cmd("thin_check", "-q", dev, check=False, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL).returncode == 0


def blocks_below(pool: Pool, region_table: str, physical: int, block_size: int) -> int:
    """How many leading data blocks of the pool lie entirely below the disk
    byte `physical` (the region table maps the PV onto the disk)."""
    covered = 0
    for s in pool.tdata:
        begin = L.logical_to_physical(region_table, s.pv_start * L.SECTOR)
        end_last = L.logical_to_physical(region_table, (s.pv_start + s.size) * L.SECTOR - 1) + 1
        if end_last <= physical:
            covered = (s.start + s.size) * L.SECTOR
            continue
        covered = s.start * L.SECTOR + max(0, physical - begin)
        break
    return covered // block_size


def high_water_physical(pool: Pool, region_table: str, u: Usage) -> int:
    """Disk byte just past the side's highest allocated data block (for the
    decoy: its pool data grows up from the front of the disk)."""
    if u.high_water == 0:
        return 0
    last = pool.tdata_to_pv(u.high_water * u.block_size - 1)
    return L.logical_to_physical(region_table, last) + 1
