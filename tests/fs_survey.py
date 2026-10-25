"""Filesystem survey: thin pool footprint under churn for several
filesystems and options, with and without a shutdown trim, for two
workloads.  Not part of the unit test suite (it takes a while); run with

    python3 -m tests.fs_survey [CYCLES]

and it prints a markdown table (results/fs-survey.md in the work dir too).
"""

import concurrent.futures
import json
import sys

from tests import vm

DISK = 8 << 30
TARGET = 0.25

VARIANTS = {
    "btrfs (ssd)": ("mkfs.btrfs -q -f --nodiscard", "noatime,nodiscard,ssd"),
    "btrfs (nossd)": ("mkfs.btrfs -q -f --nodiscard", "noatime,nodiscard,nossd"),
    "btrfs (ssd_spread)": ("mkfs.btrfs -q -f --nodiscard", "noatime,nodiscard,ssd_spread"),
    "btrfs (nodatacow)": ("mkfs.btrfs -q -f --nodiscard", "noatime,nodiscard,ssd,nodatacow"),
    "btrfs (compress-force)": ("mkfs.btrfs -q -f --nodiscard", "noatime,nodiscard,ssd,compress-force=zstd"),
    "btrfs (autodefrag)": ("mkfs.btrfs -q -f --nodiscard", "noatime,nodiscard,ssd,autodefrag"),
    "ext4": ("mkfs.ext4 -q -F", "noatime,nodiscard"),
    "xfs": ("mkfs.xfs -q -f -K", "noatime,nodiscard"),
    "f2fs": ("mkfs.f2fs -q -f -t 0", "noatime,nodiscard"),
}

SCRIPT = """
vg=churn
lvm pvcreate -qq /dev/vda
lvm vgcreate -qq $vg /dev/vda
lvm lvcreate -qq --type thin-pool -l 100%FREE --discards nopassdown -n pool $vg
size=$(lvm lvs --noheadings --units b --nosuffix -o lv_size $vg/pool | tr -d ' ')
lvm lvcreate -qq -V ${{size}}b -T $vg/pool -n vol
mkdir -p /mnt/t
{mkfs} /dev/$vg/vol
mount -o {opts} /dev/$vg/vol /mnt/t
python3 /src/tests/churn.py /mnt/t $vg/pool {cycles} {target} {reboot_every} {workload}
umount /mnt/t
lvm vgchange -an -qq $vg
"""


def run(env, variant, workload, reboot_every, cycles):
    mkfs, opts = VARIANTS[variant]
    work = vm.workdir()
    try:
        disk = vm.sparse_disk(work / "disk.img", DISK)
        rc, out = vm.ToolVM(env, work).run(
            SCRIPT.format(mkfs=mkfs, opts=opts, cycles=cycles, target=TARGET,
                          reboot_every=reboot_every, workload=workload),
            disks=[disk], timeout=7200, check=False, log=work / "log")
    finally:
        vm.cleanup(work)
    if rc != 0 or "RESULT " not in out:
        return {"error": out[-1500:]}
    rows = [json.loads(l) for l in out.splitlines() if l.startswith('{"cycle"')]
    return {"rows": rows}


def main():
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    env = vm.testenv()
    jobs = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for variant in VARIANTS:
            for workload in ("churn", "rewrite"):
                for reboot_every in (0, 5):
                    jobs[(variant, workload, reboot_every)] = pool.submit(
                        run, env, variant, workload, reboot_every, cycles)
    results = {k: f.result() for k, f in jobs.items()}
    print(json.dumps({" | ".join(map(str, k)): v for k, v in results.items()}), file=sys.stderr)

    def cell(r):
        if "error" in r:
            return "error"
        rows = r["rows"]
        hw = max(x["high_water"] for x in rows) >> 20
        last = rows[-1]["high_water"] >> 20
        peak = rows[-1]["peak_fs_used"] >> 20
        return f"{last} (max {hw}) / peak {peak}"

    print(f"\n{cycles} cycles, 8 GiB volume. Cells: high-water at the end (max seen) / peak fs usage, MiB.\n")
    print("| Filesystem | churn, no trim | churn, trim every 5 | rewrite, no trim | rewrite, trim every 5 |")
    print("|---|---|---|---|---|")
    for variant in VARIANTS:
        cells = [cell(results[(variant, w, r)]) for w in ("churn", "rewrite") for r in (0, 5)]
        print(f"| {variant} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()

