"""Frontier monitor (design.md §10.2): on every hidden boot, report the
space left between the two sides and warn when it gets small."""

import re
import unittest

from tests import fixture, vm


class Monitor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = fixture.make()

    @classmethod
    def tearDownClass(cls):
        vm.cleanup(cls.fx.work)

    def hidden_boot(self, name):
        b = self.fx.boot(name)
        self.addCleanup(b.logfile.close)
        self.addCleanup(b.close)
        b.expect("Password: ")
        b.sendline("")
        b.expect("recovery shell")
        b.sendline(self.fx.sed)
        b.sendline("/early.sh")
        b.expect("Password: ")
        b.sendline(fixture.HIDDEN_PW)
        b.expect(r"Free space between the two sides: (-?\d+) MiB\.", timeout=120)
        return b, int(b.child.match.group(1))

    def test_reports_then_warns(self):
        b, gap = self.hidden_boot("monitor-1")
        b.expect("PD-TARGET: marker=hidden")
        self.assertNotIn("inside the hidden region", b.child.before)
        # Fresh 8 GiB disk: most of it is free.
        self.assertGreater(gap, 6000)
        self.assertLess(gap, 8192)

        # Fill the decoy with 6.5 GiB of incompressible data.
        script = f"""
        printf '%s\\n' '{fixture.DECOY_PW}' | python3 -m pd open --config pd.toml --rw --password-stdin \\
            --run 'head -c 6800M /dev/urandom > $PD_ROOT/fake/home/big; sync'
        """
        vm.ToolVM(self.fx.env, self.fx.work).run(script, disks=[self.fx.disk], timeout=900)

        b, gap2 = self.hidden_boot("monitor-2")
        self.assertLess(gap2, gap - 6000)
        b.expect("WARNING: less than 10% of the disk separates the two sides")
        b.expect("Press Enter to continue booting")
        b.sendline("")
        b.expect("PD-TARGET: marker=hidden")

    def test_decoy_has_no_hidden_files(self):
        # Invariant 1: nothing hidden-specific in the decoy's boot volume.
        b = self.fx.boot("decoy-files")
        self.addCleanup(b.logfile.close)
        self.addCleanup(b.close)
        b.expect("Password: ")
        b.sendline("")
        b.expect("~ # ")
        b.sendline(". /early_env.sh; printf %s '" + fixture.DECOY_PW + "' | "
                   "argon2 \"$(cat /salt.txt)\" $ARGON2_ARGS | xxd -r -p > /run/k; "
                   "cryptsetup open --type plain --readonly $CRYPT_ARGS --offset 2048 "
                   "--size 522240 --key-file /run/k /dev/vda c; mkdir -p /m; "
                   "mount -o ro,rescue=nologreplay /dev/mapper/c /m; "
                   "m=LIST; echo \"<<$m\"; find /m -maxdepth 2 | sort; echo \"$m>>\"")
        # The terminal echoes the command; the markers only appear expanded.
        b.expect("<<LIST")
        b.expect("LIST>>")
        listing = b.child.before
        for bad in ("decoy.key", "decoy.table", "hooks", "frontier"):
            self.assertNotIn(bad, listing)
        self.assertIn("/m/late.sh", listing)


if __name__ == "__main__":
    unittest.main()
