"""Frontier monitor (design.md §10.2): on every hidden boot, report the
space left between the two sides and warn when it gets small."""

import re
import unittest

from tests import fixture, vm


def before_kexec(b) -> str:
    """What the boot printed before handing over (the target OS has warnings
    of its own)."""
    return b.child.before.split("kexec_core: Starting new kernel")[0]


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
        self.assertNotIn("WARNING", before_kexec(b))
        # Fresh 8 GiB disk: most of it is free.
        self.assertGreater(gap, 6000)
        self.assertLess(gap, 8192)

        # Fill the decoy with 6.5 GiB of incompressible data, on its XFS
        # volume: deleted and trimmed later, it leaves nothing mapped up there
        # (btrfs would keep metadata it wrote meanwhile).
        script = f"""
        printf '%s\\n' '{fixture.DECOY_PW}' | python3 -m pd open --config pd.toml --rw --password-stdin \\
            --run 'head -c 6800M /dev/urandom > $PD_ROOT/volumes/data/big; sync'
        """
        vm.ToolVM(self.fx.env, self.fx.work).run(script, disks=[self.fx.disk], timeout=900)

        b, gap2 = self.hidden_boot("monitor-2")
        self.assertLess(gap2, gap - 6000)
        # The decoy is now above the hidden side's floor (design.md §10.5).
        b.expect(r"WARNING: the decoy has data in the space reserved for this side \(\d+ MiB\)")
        b.expect("Press Enter to continue booting")
        b.sendline("")
        b.expect("WARNING: less than 10% of the disk separates the two sides")
        b.expect("Press Enter to continue booting")
        b.sendline("")
        b.expect("PD-TARGET: marker=hidden")
        b.close()

        # The decoy deletes it and trims: its data is back below the floor,
        # but its pool metadata's stale nodes still point above it (tmeta_scan).
        script = f"""
        printf '%s\\n' '{fixture.DECOY_PW}' | python3 -m pd open --config pd.toml --rw --password-stdin \\
            --run 'rm $PD_ROOT/volumes/data/big; sync; fstrim $PD_ROOT/volumes/data'
        """
        vm.ToolVM(self.fx.env, self.fx.work).run(script, disks=[self.fx.disk], timeout=900)
        b, _ = self.hidden_boot("monitor-3")
        b.expect(r"WARNING: the decoy's pool metadata still points into the space reserved for this side")
        b.expect("Press Enter to continue booting")
        self.assertNotIn("the decoy has data in the space reserved", b.child.before)
        b.sendline("")
        b.expect("PD-TARGET: marker=hidden")
        b.close()

        # pd compact on the decoy rewrites its metadata: no warnings left.
        offset = fixture.hidden_offset_sectors(self.fx)
        script = f"""
        printf '%s\\n%s\\n' '{fixture.DECOY_PW}' '{fixture.HIDDEN_PW}' | python3 -m pd compact \\
            --config pd.toml --hidden-offset {offset} --password-stdin
        """
        vm.ToolVM(self.fx.env, self.fx.work).run(script, disks=[self.fx.disk], timeout=900)
        b, _ = self.hidden_boot("monitor-4")
        b.expect("PD-TARGET: marker=hidden")
        self.assertNotIn("WARNING", before_kexec(b))

    def test_decoy_has_no_hidden_files(self):
        # Invariant 1: nothing hidden-specific in the decoy's boot volume.
        b = self.fx.boot("decoy-files")
        self.addCleanup(b.logfile.close)
        self.addCleanup(b.close)
        b.expect("Password: ")
        b.sendline("")
        b.expect("~ # ")
        b.sendline(". /early_env.sh; . /disk_class.sh; A=$(cat /.extra/credentials/argon2.cred); "
                   "printf %s '" + fixture.DECOY_PW + "' | "
                   "argon2 \"$(disk_salt /dev/vda)\" $A | xxd -r -p > /run/k; "
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
