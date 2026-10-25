"""Churn test (design.md §2.3): with discards off, a filesystem's footprint
in the thin pool must track its peak usage, not its total writes.  Run for
btrfs and ext4; this decides which filesystems are safe on pd."""

import json
import os
import unittest

from tests import vm

DISK = 8 << 30
CYCLES = int(os.environ.get("PD_CHURN_CYCLES", "60"))
TARGET = 0.25  # fill each cycle to 25% of the filesystem

SCRIPT = """
fs={fs}
vg=churn
lvm pvcreate -qq /dev/vda
lvm vgcreate -qq $vg /dev/vda
lvm lvcreate -qq --type thin-pool -l 100%FREE --discards ignore -n pool $vg
size=$(lvm lvs --noheadings --units b --nosuffix -o lv_size $vg/pool | tr -d ' ')
lvm lvcreate -qq -V ${{size}}b -T $vg/pool -n vol
mkdir -p /mnt/t
case $fs in
btrfs) mkfs.btrfs -q -f --nodiscard /dev/$vg/vol; opts=noatime,nodiscard,{btrfs_opts} ;;
ext4) mkfs.ext4 -q -E nodiscard /dev/$vg/vol; opts=noatime,nodiscard ;;
esac
mount -o $opts /dev/$vg/vol /mnt/t
# Discards must be refused at the pool.
if fstrim /mnt/t 2>/dev/null; then echo "DISCARD-ACCEPTED"; else echo "DISCARD-REFUSED"; fi
python3 /src/tests/churn.py /mnt/t $vg/pool {cycles} {target}
umount /mnt/t
lvm vgchange -an -qq $vg
"""


class Churn(unittest.TestCase):
    results = {}

    @classmethod
    def setUpClass(cls):
        cls.env = vm.testenv()

    def run_fs(self, fs):
        work = vm.workdir()
        try:
            disk = vm.sparse_disk(work / "disk.img", DISK)
            _, out = vm.ToolVM(self.env, work).run(
                SCRIPT.format(fs=fs, cycles=CYCLES, target=TARGET,
                              btrfs_opts=os.environ.get("PD_CHURN_BTRFS_OPTS", "ssd")), disks=[disk], timeout=1800,
                log=work / "churn.log")
        finally:
            vm.cleanup(work)
        self.assertIn("DISCARD-REFUSED", out)
        rows = [json.loads(l) for l in out.splitlines() if l.startswith('{"cycle"')]
        print(f"\n{fs} trajectory (cycle: pool MiB / fs used MiB):")
        for r in rows[::5] + rows[-1:]:
            print(f"  {r['cycle']}: {r['pool_used'] >> 20} / {r['fs_used'] >> 20}")
        result = json.loads(out.split("RESULT ", 1)[1].splitlines()[0])
        Churn.results[fs] = result
        print(f"\n{fs}: pool_used={result['pool_used'] >> 20} MiB "
              f"peak_fs_used={result['peak_fs_used'] >> 20} MiB "
              f"fs_size={result['fs_size'] >> 20} MiB")
        return result

    def test_btrfs(self):
        r = self.run_fs("btrfs")
        # The pool footprint must stay near peak usage (allow 2x for block
        # group slack and metadata), far below the total written.
        self.assertLess(r["pool_used"], 2 * r["peak_fs_used"] + (512 << 20))

    def test_ext4(self):
        # Measured: ext4 spreads allocations over the whole device, so its
        # pool footprint reaches the full virtual size within ~15 cycles
        # while using under 1 GiB.  ext4 is unsafe on pd; this test documents
        # that and fails loudly if it ever stops being true.
        r = self.run_fs("ext4")
        self.assertGreater(r["pool_used"], 0.9 * r["fs_size"])


if __name__ == "__main__":
    unittest.main()

