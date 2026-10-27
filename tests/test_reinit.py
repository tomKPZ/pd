"""Re-initializing a decoy: refused by default; with --force and the hidden
side's offset and password, the decoy is re-created with its pool metadata
at the front, and both sides still boot."""

import unittest

from tests import fixture, vm


class ReinitDecoy(unittest.TestCase):
    def test_reinit_guard_and_hidden_survives(self):
        fx = fixture.make()
        self.addCleanup(vm.cleanup, fx.work)
        offset = fixture.hidden_offset_sectors(fx)
        tool = vm.ToolVM(fx.env, fx.work)

        rc, out = tool.run(f"""
        printf '%s\\n' '{fixture.DECOY_PW}' | python3 -m pd init-decoy --config pd.toml --password-stdin
        """, disks=[fx.disk], check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("already has a decoy side", out)

        rc, out = tool.run(f"""
        printf '%s\\n' '{fixture.DECOY_PW}' | python3 -m pd init-decoy --config pd.toml --password-stdin --force
        """, disks=[fx.disk], check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("--no-hidden or --hidden-offset", out)

        tool.run(f"""
        printf '%s\\n%s\\n' '{fixture.DECOY_PW}' '{fixture.HIDDEN_PW}' | python3 -m pd init-decoy --config pd.toml \\
            --password-stdin --force --hidden-offset {offset} --run 'sh /work/install.sh decoy2'
        """, disks=[fx.disk], log=fx.work / "reinit.log")

        b = fx.boot("reinit-decoy")
        self.addCleanup(b.logfile.close)
        self.addCleanup(b.close)
        b.expect("Password: ")
        b.sendline(fixture.DECOY_PW)
        b.expect("PD-TARGET: marker=decoy2", timeout=120)

        b = fx.boot("reinit-hidden")
        self.addCleanup(b.logfile.close)
        self.addCleanup(b.close)
        b.expect("Password: ")
        b.sendline("")
        b.expect("recovery shell")
        b.sendline(fx.sed)
        b.sendline("/early.sh")
        b.expect("Password: ")
        b.sendline(fixture.HIDDEN_PW)
        # The hidden side got the new decoy's key: its frontier monitor reads it.
        b.expect(r"Free space between the two sides: \d+ MiB", timeout=120)
        self.assertNotIn("doesn't open with the key", b.child.before)
        b.expect("PD-TARGET: marker=hidden", timeout=120)


if __name__ == "__main__":
    unittest.main()
