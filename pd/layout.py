"""Disk layout arithmetic.  Pure functions, no I/O; see docs/design.md §3."""

import dataclasses

SECTOR = 512
KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB

ZERO_HEADER = 1 * MIB
PLAIN_SIZE = 255 * MIB
DATA_START = ZERO_HEADER + PLAIN_SIZE  # 256 MiB
REVERSE_CHUNK = 256 * MIB
ALIGN = 1 * MIB

DECOY_PLAIN_OFFSET = ZERO_HEADER  # sector 2048


def sectors(nbytes: int) -> int:
    assert nbytes % SECTOR == 0, nbytes
    return nbytes // SECTOR


def align_down(n: int, a: int) -> int:
    return n // a * a


def disk_size(raw_size: int) -> int:
    """D: usable disk size, rounded down to 1 MiB."""
    return align_down(raw_size, ALIGN)


def linear_table(start: int, end: int, dev: str = "DISK") -> str:
    """dm table mapping [start, end) of dev linearly."""
    assert 0 <= start < end
    return f"0 {sectors(end - start)} linear {dev} {sectors(start)}\n"


def reversed_table(start: int, end: int, chunk: int = REVERSE_CHUNK, dev: str = "DISK") -> str:
    """dm table mapping [start, end) of dev in reverse order of chunks.

    Logical chunk 0 is the physically last whole chunk below `end`.  A partial
    chunk at the low end (when the region isn't a multiple of `chunk`) is left
    unmapped.
    """
    assert 0 <= start < end and chunk % SECTOR == 0
    n = (end - start) // chunk
    assert n > 0, "region smaller than one chunk"
    spc = sectors(chunk)
    lines = []
    for i in range(n):
        phys = end - (i + 1) * chunk
        lines.append(f"{i * spc} {spc} linear {dev} {sectors(phys)}")
    return "\n".join(lines) + "\n"


def parse_table(table: str) -> list[tuple[int, int, int]]:
    """Parse a linear dm table into (logical_sector, length, physical_sector)."""
    out = []
    for line in table.splitlines():
        if not line.strip():
            continue
        lstart, length, target, _dev, pstart = line.split()
        assert target == "linear", line
        out.append((int(lstart), int(length), int(pstart)))
    return out


def table_physical_range(table: str) -> tuple[int, int]:
    """Smallest physical byte range [lo, hi) covered by a table."""
    segs = parse_table(table)
    lo = min(p for _, _, p in segs)
    hi = max(p + n for _, n, p in segs)
    return lo * SECTOR, hi * SECTOR


def logical_to_physical(table: str, logical: int) -> int:
    """Map a logical byte offset through a linear dm table."""
    s = logical // SECTOR
    for lstart, length, pstart in parse_table(table):
        if lstart <= s < lstart + length:
            return (pstart + s - lstart) * SECTOR + logical % SECTOR
    raise ValueError(f"offset {logical} not mapped")


@dataclasses.dataclass(frozen=True)
class HiddenLayout:
    plain_offset: int  # bytes
    data_start: int
    data_end: int

    @property
    def plain_offset_sectors(self) -> int:
        return sectors(self.plain_offset)

    def data_table(self) -> str:
        return reversed_table(self.data_start, self.data_end)


def hidden_layout(top: int, memorable: bool = False) -> HiddenLayout:
    """Place the hidden side below `top` (H: start of the decoy's end-of-disk
    metadata, or D - 1 MiB if there is none).

    With `memorable`, the plain volume's start sector is rounded down to a
    multiple of 10**6 sectors so the offset is easy to remember.
    """
    plain = align_down(top - PLAIN_SIZE, ALIGN)
    if memorable:
        plain = align_down(sectors(plain), 10**6) * SECTOR
    data_end = plain
    if data_end - DATA_START < REVERSE_CHUNK:
        raise ValueError("disk too small for a hidden side")
    return HiddenLayout(plain_offset=plain, data_start=DATA_START, data_end=data_end)


def hidden_boot_command(layout: HiddenLayout) -> str:
    return f"sed -i s/{sectors(DECOY_PLAIN_OFFSET)}/{layout.plain_offset_sectors}/ /early_env.sh"

