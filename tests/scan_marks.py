"""Count leftovers of the decoy's old data above a boundary (run in the tool
VM; for test_compact).  The fill files' 4 KiB blocks each start with MARK,
so any block of them left in the decoy's pool data, mapped or not, is found
by reading the pool's data device with the decoy's key.

Usage:
  scan_marks.py pd CONFIG PASSWORD (--boundary-mib N | --floor)
  scan_marks.py stock DEVICE PASSWORD --boundary-mib N
Prints "MARKS <count>".
"""

import sys

from pd import layout as L
from pd import setup, thinpool
from pd.config import Config

MARK = b"PDMARK-"


def count(pool: thinpool.Pool, start: int) -> int:
    n = 0
    with thinpool.mapped(pool, writable=False) as devs, open(devs["tdata"], "rb") as f:
        f.seek(start)
        while True:
            buf = f.read(16 << 20)
            if not buf:
                break
            n += sum(1 for off in range(0, len(buf), 4096) if buf[off:off + len(MARK)] == MARK)
    return n


def main():
    kind, target, password, mode = sys.argv[1:5]
    pw = password.encode()
    if kind == "pd":
        cfg = Config.load(target)
        key = setup.boot_key(cfg, L.DECOY_PLAIN_OFFSET, pw)
        with setup.boot_volume(cfg.disk, L.DECOY_PLAIN_OFFSET, key, create=False) as mnt:
            data_key, table = (mnt / "data.key").read_bytes(), (mnt / "table.txt").read_text()
        with setup.data_region(cfg.disk, table, data_key, readonly=True) as dev:
            pool = thinpool.find_pool(cfg.vg, str(dev))
            if mode == "--floor":
                d = setup.check_disk(cfg.disk)
                floor = setup.hidden_floor(cfg, setup.lvm_segments(cfg.vg), pool.pe_start * L.SECTOR,
                                           L.table_physical_range(table)[0], d)
                with thinpool.mapped(pool, writable=False) as devs:
                    bs = thinpool.usage(thinpool.dump(devs["tmeta"])).block_size
                start = thinpool.blocks_below(pool, table.replace("DISK", cfg.disk), floor, bs) * bs
            else:
                start = int(sys.argv[5]) * L.MIB
            print("MARKS", count(pool, start))
    else:
        with setup.stock_decoy_opened(target, pw, None) as dev:
            vg = setup.lvm_output("pvs", "--noheadings", "-o", "vg_name", dev)
            pool = thinpool.find_pool(vg, str(dev))
            print("MARKS", count(pool, int(sys.argv[5]) * L.MIB))


main()

