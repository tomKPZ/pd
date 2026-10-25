"""Churn workload, run inside the tool VM: repeatedly fill a filesystem to a
target usage and delete a random part of it, and report how much of the thin
pool the filesystem has touched compared with how much it actually uses.

Usage: churn.py MOUNTPOINT POOL(vg/lv) CYCLES TARGET_FRACTION [REBOOT_EVERY]

With REBOOT_EVERY=N, every N cycles simulate a shutdown with fstrim and a
reboot: fstrim, unmount, deactivate and reactivate the pool (which resets
dm-thin's allocation cursor), mount again.
"""

import json
import os
import random
import shutil
import subprocess
import sys

mnt, pool, cycles, target = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
reboot_every = int(sys.argv[5]) if len(sys.argv) > 5 else 0
rng = random.Random(1234)
blob = os.urandom(64 << 20)  # incompressible source data


def pool_used() -> int:
    out = subprocess.run(
        ["lvm", "lvs", "--noheadings", "--units", "b", "--nosuffix",
         "-o", "lv_size,data_percent", pool],
        check=True, stdout=subprocess.PIPE, text=True,
        env=dict(os.environ, LVM_SUPPRESS_FD_WARNINGS="1")).stdout.split()
    return int(float(out[0]) * float(out[1]) / 100)


def high_water() -> int:
    """Bytes up to and including the highest allocated pool block, read from
    the pool metadata (pool must be inactive)."""
    vg, lv = pool.split("/")
    env = dict(os.environ, LVM_SUPPRESS_FD_WARNINGS="1")
    subprocess.run(["lvm", "lvchange", "-ay", "-qq", "-y", f"{vg}/{lv}_tmeta"], check=True, env=env)
    try:
        xml = subprocess.run(["thin_dump", f"/dev/{vg}/{lv}_tmeta"], check=True,
                             stdout=subprocess.PIPE, text=True).stdout
    finally:
        subprocess.run(["lvm", "lvchange", "-an", "-qq", f"{vg}/{lv}_tmeta"], check=True, env=env)
    import re
    bs = int(re.search(r'data_block_size="(\d+)"', xml).group(1)) * 512
    top = 0
    for m in re.finditer(r'data_begin="(\d+)" length="(\d+)"', xml):
        top = max(top, int(m.group(1)) + int(m.group(2)))
    for m in re.finditer(r'data_block="(\d+)"', xml):
        top = max(top, int(m.group(1)) + 1)
    return top * bs


hw = 0


def reboot():
    global hw
    dev = subprocess.run(["findmnt", "-n", "-o", "SOURCE", mnt], check=True,
                         stdout=subprocess.PIPE, text=True).stdout.strip()
    opts = subprocess.run(["findmnt", "-n", "-o", "OPTIONS", mnt], check=True,
                          stdout=subprocess.PIPE, text=True).stdout.strip()
    env = dict(os.environ, LVM_SUPPRESS_FD_WARNINGS="1")
    subprocess.run(["fstrim", mnt], check=True)
    subprocess.run(["umount", mnt], check=True)
    vg = pool.split("/")[0]
    subprocess.run(["lvm", "vgchange", "-an", "-qq", vg], check=True, env=env)
    hw = high_water()
    subprocess.run(["lvm", "vgchange", "-ay", "-qq", vg], check=True, env=env)
    subprocess.run(["mount", "-o", opts, dev, mnt], check=True)


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
    if reboot_every and (cycle + 1) % reboot_every == 0:
        reboot()
    used = pool_used()
    rows.append({"cycle": cycle, "fs_used": fs_used(), "peak_fs_used": peak,
                 "pool_used": used, "high_water": max(hw, used if not reboot_every else hw),
                 "fs_size": fs_size()})
    print(json.dumps(rows[-1]), flush=True)

print("RESULT " + json.dumps(rows[-1]), flush=True)

