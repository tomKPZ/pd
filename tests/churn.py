"""Churn workload, run inside the tool VM: repeatedly fill a filesystem to a
target usage and delete a random part of it, and report how much of the thin
pool the filesystem has touched compared with how much it actually uses.

Usage: churn.py MOUNTPOINT POOL(vg/lv) CYCLES TARGET_FRACTION
"""

import json
import os
import random
import shutil
import subprocess
import sys

mnt, pool, cycles, target = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
rng = random.Random(1234)
blob = os.urandom(64 << 20)  # incompressible source data


def pool_used() -> int:
    out = subprocess.run(
        ["lvm", "lvs", "--noheadings", "--units", "b", "--nosuffix",
         "-o", "lv_size,data_percent", pool],
        check=True, stdout=subprocess.PIPE, text=True,
        env=dict(os.environ, LVM_SUPPRESS_FD_WARNINGS="1")).stdout.split()
    return int(float(out[0]) * float(out[1]) / 100)


def fs_used() -> int:
    st = os.statvfs(mnt)
    return (st.f_blocks - st.f_bfree) * st.f_frsize


def fs_size() -> int:
    st = os.statvfs(mnt)
    return st.f_blocks * st.f_frsize


files = []
seq = 0
peak = 0
rows = []
for cycle in range(cycles):
    # Fill to the target, spread over many directories.
    while fs_used() < target * fs_size():
        d = os.path.join(mnt, f"d{rng.randrange(64)}", f"e{rng.randrange(16)}")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"f{seq}")
        seq += 1
        size = rng.choice([4 << 10, 64 << 10, 1 << 20, 8 << 20, 32 << 20])
        with open(path, "wb") as f:
            off = rng.randrange(len(blob) - size)
            f.write(blob[off:off + size])
        files.append(path)
    os.sync()
    peak = max(peak, fs_used())
    # Delete about half, at random.
    rng.shuffle(files)
    keep = len(files) // 2
    for path in files[keep:]:
        os.unlink(path)
    files = files[:keep]
    os.sync()
    rows.append({"cycle": cycle, "fs_used": fs_used(), "peak_fs_used": peak,
                 "pool_used": pool_used(), "fs_size": fs_size()})
    print(json.dumps(rows[-1]), flush=True)

print("RESULT " + json.dumps(rows[-1]), flush=True)

