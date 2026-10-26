"""Safety properties that need a real setup: booting after an unclean
shutdown, the hidden side's footprint cap, the disk's zeroed edges, and pool
and swap settings."""

import re
import unittest

from pd import layout as L
from tests import fixture, vm


class Safety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = fixture.make()

    @classmethod
    def tearDownClass(cls):
        vm.cleanup(cls.fx.work)

    def test_boot_after_unclean_shutdown(self):
        # Leave a dirty btrfs log tree in the decoy's @boot: fsync a new file,
        # then reset the machine without unmounting.
        fx = self.fx
        script = f"""
        printf '%s\\n' '{fixture.DECOY_PW}' | python3 -m pd open --config pd.toml --rw --password-stdin \\
            --run 'python3 -c "import os; f = os.open(\\"$PD_ROOT/fake/boot/dirty\\", os.O_CREAT | os.O_WRONLY); os.write(f, b\\"x\\"); os.fsync(f)"; echo b > /proc/sysrq-trigger'
        """
        vm.ToolVM(fx.env, fx.work).run(script, disks=[fx.disk], check=False,
                                       log=fx.work / "crash.log")
        # Confirm the log tree really is dirty (log_root != 0), without
        # mounting (a read-write mount would replay it).
        check = f"""
        python3 - <<'EOF'
import pathlib
from pd import layout as L, setup
from pd.config import Config
from pd.util import argon2, cmd_output
cfg = Config.load("pd.toml")
key = argon2(b"{fixture.DECOY_PW}", cfg.salt, cfg.argon2)
with setup.boot_volume(cfg.disk, L.DECOY_PLAIN_OFFSET, key, create=False) as mnt:
    data_key = (mnt / "data.key").read_bytes()
    table = (mnt / "table.txt").read_text()
with setup.data_region(cfg.disk, table, data_key, readonly=False):
    with setup.vg_active(cfg.vg):
        out = cmd_output("btrfs", "inspect-internal", "dump-super", "/dev/sysvg/root")
        print([l for l in out.splitlines() if l.startswith("log_root\\t")][0].replace("\\t", " "))
EOF
        """
        _, out = vm.ToolVM(fx.env, fx.work).run(check, disks=[fx.disk])
        log_root = int(out.split("log_root ")[1].split()[0])
        self.assertNotEqual(log_root, 0, "expected a dirty log tree")
        b = fx.boot("after-crash")
        self.addCleanup(b.logfile.close)
        self.addCleanup(b.close)
        b.expect("Password: ")
        b.sendline(fixture.DECOY_PW)
        b.expect("PD-TARGET: marker=decoy", timeout=120)

    def test_hidden_thin_lvs_are_capped(self):
        fx = self.fx
        offset = fixture.hidden_offset_sectors(fx)
        script = f"""
        printf '%s\\n' '{fixture.HIDDEN_PW}' | python3 -m pd open --config pd.toml --rw --password-stdin \\
            --offset {offset} --run 'lvm lvs --noheadings --units b --nosuffix -o lv_name,lv_size sysvg; findmnt -n -o FSTYPE $PD_ROOT/volumes/data'
        """
        _, out = vm.ToolVM(fx.env, fx.work).run(script, disks=[fx.disk])
        sizes = dict(line.split() for line in out.splitlines()
                     if line.strip().startswith(("root ", "data ", "thinpool ")))
        disk = L.disk_size(fx.disk.stat().st_size)
        # The cap covers all hidden thin LVs together.
        self.assertLessEqual(int(sizes["root"]) + int(sizes["data"]), int(disk * fx.cfg.hidden_cap))
        self.assertLess(int(sizes["root"]), int(sizes["thinpool"]))
        # The extra volume exists with the configured filesystem.
        self.assertIn("xfs", out)


    def test_disk_edges_and_pool_settings(self):
        fx = self.fx
        d = L.disk_size(fx.disk.stat().st_size)
        # The first and last MiB stay zero (L.ZERO_HEADER, L.ZERO_TRAILER).
        with open(fx.disk, "rb") as f:
            self.assertEqual(f.read(L.ZERO_HEADER), bytes(L.ZERO_HEADER))
            f.seek(d - L.ZERO_TRAILER)
            self.assertEqual(f.read(L.ZERO_TRAILER), bytes(L.ZERO_TRAILER))
        _, out = vm.ToolVM(fx.env, fx.work).run(f"""
        printf '%s\\n' '{fixture.DECOY_PW}' | python3 -m pd open --config pd.toml --rw --password-stdin \\
            --run 'dmsetup table cryptlvm-raw; lvm lvs --noheadings --binary -o lv_name,zero sysvg'
        """, disks=[fx.disk])
        # The decoy's data region ends where the zero trailer starts.
        start, length, _, _, offset = next(l.split() for l in out.splitlines() if " linear " in l)
        self.assertEqual((int(offset) + int(length)) * L.SECTOR, L.data_end(d))
        # The pools don't zero new chunks.
        zero = dict(l.split() for l in out.splitlines() if l.strip().startswith("thinpool "))
        self.assertEqual(zero["thinpool"], "0")

    def test_decoy_lvm_history_is_ordinary(self):
        """LVM records each command line in the on-disk metadata (design.md
        §6.1): the decoy's show only the stock commands, without pd's
        settings."""
        fx = self.fx
        _, out = vm.ToolVM(fx.env, fx.work).run(f"""
        printf '%s\\n' '{fixture.DECOY_PW}' | python3 -m pd open --config pd.toml --password-stdin \\
            --run 'lvm pvck --dump metadata_all -f /run/meta.txt /dev/mapper/cryptlvm && cat /run/meta.txt'
        """, disks=[fx.disk])
        descriptions = [line.split("=", 1)[1].strip() for line in out.splitlines()
                        if line.strip().startswith("description = ")]
        self.assertTrue(descriptions, out[-2000:])
        commands = set()
        for d in descriptions:
            self.assertNotIn("--config", d)
            self.assertNotIn("--devices", d)
            self.assertNotIn("-qq", d)
            commands.add(re.match(r'"Write(?:\[\d+\])? from (\S+)', d).group(1))
        self.assertLessEqual(commands, {"pvcreate", "vgcreate", "lvcreate"}, descriptions)


class SwapDefault(unittest.TestCase):
    def test_swap_defaults_to_ram_size(self):
        env = vm.testenv()
        work = vm.workdir()
        self.addCleanup(vm.cleanup, work)
        _, out = vm.ToolVM(env, work).run("""
        python3 -c 'from pd import setup; from pd.config import Config; print("SWAP", setup.swap_size(Config(disk="x")), setup.swap_size(Config(disk="x", swap_size=0)))'
        awk '/MemTotal/ { print "MEM", $2 * 1024 }' /proc/meminfo
        """)
        swap, none = (int(x) for x in out.split("SWAP ", 1)[1].split()[:2])
        mem = int(out.split("MEM ", 1)[1].split()[0])
        self.assertEqual(none, 0)
        self.assertEqual(swap % L.GIB, 0)
        self.assertTrue(mem <= swap < mem + L.GIB)


class TrimOnShutdown(unittest.TestCase):
    """fstrim reaches the thin pool (which reclaims space) but never the
    disk: dm-crypt has no allow_discards, and the pool is nopassdown."""

    def test_trim_reclaims_pool_space_without_touching_disk(self):
        fx = fixture.make()
        self.addCleanup(vm.cleanup, fx.work)
        allocated = lambda: fx.disk.stat().st_blocks
        before = allocated()
        script = f"""
        printf '%s\\n' '{fixture.DECOY_PW}' | python3 -m pd open --config pd.toml --rw --password-stdin --run '
            pool() {{ lvm lvs --noheadings -o data_percent sysvg/thinpool | tr -d " "; }}
            head -c 1G /dev/urandom > $PD_ROOT/fake/home/big; sync
            echo "POOL-FULL $(pool)"
            rm $PD_ROOT/fake/home/big; sync
            echo "POOL-DELETED $(pool)"
            fstrim -v $PD_ROOT/fake/home
            echo "POOL-TRIMMED $(pool)"
            echo "CRYPT-DISCARD $(cat /sys/block/$(basename $(readlink -f /dev/mapper/cryptlvm))/queue/discard_max_bytes)"
        '
        """
        _, out = vm.ToolVM(fx.env, fx.work).run(script, disks=[fx.disk])
        vals = {k: float(v) for k, v in
                (l.split()[:2] for l in out.splitlines() if l.startswith("POOL-"))}
        # Deleting doesn't free pool space; trimming does.
        self.assertAlmostEqual(vals["POOL-DELETED"], vals["POOL-FULL"], delta=1)
        self.assertLess(vals["POOL-TRIMMED"], vals["POOL-FULL"] - 5)
        # dm-crypt advertises no discard support, and the image (attached
        # with discard=unmap, so any discard would punch holes) is intact.
        self.assertIn("CRYPT-DISCARD 0", out)
        self.assertGreaterEqual(allocated(), before)


if __name__ == "__main__":
    unittest.main()

