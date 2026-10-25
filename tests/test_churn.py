"""Churn test (design.md §2.3): with discards off, a filesystem's footprint
in the thin pool must track its peak usage, not its total writes.  Run for
btrfs and XFS (the filesystems pd uses), and for ext4, which pd doesn't
support because of what these tests show (tests/fs_survey.py compares more)."""

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
lvm lvcreate -qq --type thin-pool -l 100%FREE --discards {discards} -n pool $vg
size=$(lvm lvs --noheadings --units b --nosuffix -o lv_size $vg/pool | tr -d ' ')
lvm lvcreate -qq -V ${{size}}b -T $vg/pool -n vol
mkdir -p /mnt/t
case $fs in
btrfs) mkfs.btrfs -q -f --nodiscard /dev/$vg/vol; opts=noatime,nodiscard,{btrfs_opts} ;;
xfs) mkfs.xfs -q -f -K /dev/$vg/vol; opts=noatime,nodiscard ;;
ext4) mkfs.ext4 -q -E nodiscard /dev/$vg/vol; opts=noatime,nodiscard ;;
esac
mount -o $opts /dev/$vg/vol /mnt/t
if fstrim /mnt/t 2>/dev/null; then echo "DISCARD-ACCEPTED"; else echo "DISCARD-REFUSED"; fi
python3 /src/tests/churn.py /mnt/t $vg/pool {cycles} {target} {reboot_every}
umount /mnt/t
lvm vgchange -an -qq $vg
"""


class Churn(unittest.TestCase):
    results = {}

    @classmethod
    def setUpClass(cls):
        cls.env = vm.testenv()

    def run_fs(self, fs, reboot_every=0):
        work = vm.workdir()
        try:
            disk = vm.sparse_disk(work / "disk.img", DISK)
            _, out = vm.ToolVM(self.env, work).run(
                SCRIPT.format(fs=fs, cycles=CYCLES, target=TARGET, reboot_every=reboot_every,
                              discards="nopassdown" if reboot_every else "ignore",
                              btrfs_opts=os.environ.get("PD_CHURN_BTRFS_OPTS", "ssd")), disks=[disk], timeout=1800,
                log=work / "churn.log")
        finally:
            vm.cleanup(work)
        self.assertIn("DISCARD-ACCEPTED" if reboot_every else "DISCARD-REFUSED", out)
        rows = [json.loads(l) for l in out.splitlines() if l.startswith('{"cycle"')]
        label = f"{fs} (fstrim + reboot every {reboot_every})" if reboot_every else fs
        print(f"\n{label} trajectory (cycle: pool used / pool high-water / fs used, MiB):")
        for r in rows[::5] + rows[-1:]:
            print(f"  {r['cycle']}: {r['pool_used'] >> 20} / {r['high_water'] >> 20} / {r['fs_used'] >> 20}")
        result = json.loads(out.split("RESULT ", 1)[1].splitlines()[0])
        if reboot_every:
            self.first_session_high_water = rows[reboot_every - 1]["high_water"]
        Churn.results[label] = result
        print(f"\n{label}: pool_used={result['pool_used'] >> 20} MiB "
              f"high_water={result['high_water'] >> 20} MiB "
              f"peak_fs_used={result['peak_fs_used'] >> 20} MiB "
              f"fs_size={result['fs_size'] >> 20} MiB")
        return result

    def test_btrfs(self):
        # Measured: about 1.5x peak usage after a few cycles, then flat
        # (3.2 GiB at both 60 and 200 cycles), far below the ~60 GiB written;
        # see design.md §2.3.
        r = self.run_fs("btrfs")
        self.assertLess(r["pool_used"], 1.5 * r["peak_fs_used"] + (512 << 20))

    def test_xfs(self):
        # Measured: 3.5 GiB at 60 cycles (about 1.7x peak usage), creeping
        # slowly (3.8 GiB at 200).
        r = self.run_fs("xfs")
        self.assertLess(r["pool_used"], 2 * r["peak_fs_used"] + (512 << 20))

    def test_ext4(self):
        # Measured: ext4 spreads allocations over the whole device, so without
        # the shutdown trim its pool footprint reaches the full virtual size
        # within ~15 cycles while using under 1 GiB.  That's why pd doesn't
        # support ext4; this test fails loudly if it ever stops being true.
        r = self.run_fs("ext4")
        self.assertGreater(r["pool_used"], 0.9 * r["fs_size"])


    def test_btrfs_trim_on_shutdown(self):
        # fstrim at shutdown with a nopassdown pool: the pool reclaims what
        # the filesystem freed, and the next activation allocates from the
        # front again.
        r = self.run_fs("btrfs", reboot_every=5)
        self.assertLess(r["high_water"], 2 * r["peak_fs_used"] + (512 << 20))

    def test_xfs_trim_on_shutdown(self):
        r = self.run_fs("xfs", reboot_every=5)
        self.assertLess(r["high_water"], 2 * r["peak_fs_used"] + (512 << 20))

    def test_ext4_trim_on_shutdown(self):
        # Measured: the first session (5 cycles, no trim yet) spreads ext4's
        # metadata and data over ~5 GiB of the pool; later sessions refill
        # holes from the front, so the high-water mark stops growing.
        r = self.run_fs("ext4", reboot_every=5)
        self.assertLessEqual(r["high_water"], self.first_session_high_water * 1.05)


if __name__ == "__main__":
    unittest.main()
