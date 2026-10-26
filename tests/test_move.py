"""pd move (docs/design.md §10.4): move both sides to a bigger disk, then to a
smaller one.  After each move, both sides' files are intact, the decoy's
btrfs root has grown or shrunk with the disk, and both sides boot from the
new disk with the boot USB unchanged and the new hidden offset."""

import re
import unittest

from pd import layout as L
from tests import fixture, vm

DATA = """
mkdir -p "$PD_ROOT/fake/home/t"
for i in 1 2 3; do head -c 100M /dev/urandom > "$PD_ROOT/fake/home/t/f$i"; done
(cd "$PD_ROOT/fake/home/t" && sha256sum f* > /work/sums-$1.txt)
"""


class Move(unittest.TestCase):
    def check_and_boot(self, fx, disk, offset, label):
        dpw, hpw = fixture.DECOY_PW, fixture.HIDDEN_PW
        _, out = vm.ToolVM(fx.env, fx.work).run(f"""
        printf '%s\\n' '{dpw}' | python3 -m pd open --config pd.toml --password-stdin --run \\
            'cd fake/home/t && sha256sum -c --quiet /work/sums-decoy.txt && echo DECOY-OK; df -B1 --output=size /mnt/pd/fake | tail -1 | sed "s/^/SIZE /"'
        printf '%s\\n' '{hpw}' | python3 -m pd open --config pd.toml --password-stdin --offset {offset} --run \\
            'cd fake/home/t && sha256sum -c --quiet /work/sums-hidden.txt && echo HIDDEN-OK'
        """, disks=[disk], log=fx.work / f"check-{label}.log")
        self.assertIn("DECOY-OK", out)
        self.assertIn("HIDDEN-OK", out)
        size = int(re.search(r"^SIZE (\d+)", out, re.M).group(1))
        sed = f"sed -i s/2048/{offset}/ /early_env.sh"
        moved = fixture.Fixture(env=fx.env, work=fx.work, disk=disk, cfg=fx.cfg, sed=sed, setup_log="")
        b = moved.boot(f"decoy-{label}")
        self.addCleanup(b.logfile.close)
        b.expect("Password: ")
        b.sendline(dpw)
        b.expect("PD-TARGET: marker=decoy", timeout=120)
        b.close()
        b = moved.boot(f"hidden-{label}")
        self.addCleanup(b.logfile.close)
        b.expect("Password: ")
        b.sendline("")
        b.expect("recovery shell")
        b.sendline(sed)
        b.sendline("/early.sh")
        b.expect("Password: ")
        b.sendline(hpw)
        b.expect(r"Free space between the two sides: \d+ MiB", timeout=120)
        b.expect("PD-TARGET: marker=hidden", timeout=120)
        b.close()
        return size

    def move(self, fx, old, new, offset, label):
        dpw, hpw = fixture.DECOY_PW, fixture.HIDDEN_PW
        _, out = vm.ToolVM(fx.env, fx.work).run(f"""
        printf '%s\\n%s\\n' '{dpw}' '{hpw}' | python3 -m pd move --config pd.toml --to /dev/vdb \\
            --hidden-offset {offset} --no-randomize --password-stdin
        """, disks=[old, new], timeout=1800, log=fx.work / f"move-{label}.log")
        self.assertIn("Moved.", out)
        return int(re.search(r"s/2048/(\d+)/", out).group(1))

    def test_grow_then_shrink(self):
        fx = fixture.make()
        self.addCleanup(vm.cleanup, fx.work)
        offset = fixture.hidden_offset_sectors(fx)
        (fx.work / "data.sh").write_text(DATA)
        vm.ToolVM(fx.env, fx.work).run(f"""
        printf '%s\\n' '{fixture.DECOY_PW}' | python3 -m pd open --config pd.toml --rw --password-stdin \\
            --run 'sh /work/data.sh decoy'
        printf '%s\\n' '{fixture.HIDDEN_PW}' | python3 -m pd open --config pd.toml --rw --password-stdin \\
            --offset {offset} --run 'sh /work/data.sh hidden'
        """, disks=[fx.disk], log=fx.work / "data.log")
        small_size = self.check_and_boot(fx, fx.disk, offset, "original")

        big = vm.sparse_disk(fx.work / "big.img", 12 << 30)
        offset = self.move(fx, fx.disk, big, offset, "grow")
        big_size = self.check_and_boot(fx, big, offset, "grow")
        self.assertGreater(big_size, small_size + 3 * L.GIB)

        smaller = vm.sparse_disk(fx.work / "smaller.img", 6 << 30)
        offset = self.move(fx, big, smaller, offset, "shrink")
        smaller_size = self.check_and_boot(fx, smaller, offset, "shrink")
        # The decoy's btrfs root shrank back to the new pool's size.
        self.assertLess(smaller_size, 6 * L.GIB)


if __name__ == "__main__":
    unittest.main()

