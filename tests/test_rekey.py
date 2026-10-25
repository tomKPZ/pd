"""pd rekey: change both passwords and the salt, then boot both sides with a
USB built for the new salt."""

import re
import secrets
import unittest

from pd import usb
from tests import fixture, vm

NEW_DECOY = "new decoy password"
NEW_HIDDEN = "new hidden password"


class Rekey(unittest.TestCase):
    def test_rekey_both_sides_with_new_salt(self):
        fx = fixture.make()
        self.addCleanup(vm.cleanup, fx.work)
        salt = secrets.token_hex(32)
        offset = fixture.hidden_offset_sectors(fx)
        vm.ToolVM(fx.env, fx.work).run(f"""
        printf '%s\\n%s\\n' '{fixture.DECOY_PW}' '{NEW_DECOY}' | python3 -m pd rekey --config pd.toml --new-salt {salt} --password-stdin
        printf '%s\\n%s\\n' '{fixture.HIDDEN_PW}' '{NEW_HIDDEN}' | python3 -m pd rekey --config pd.toml --new-salt {salt} --offset {offset} --password-stdin
        """, disks=[fx.disk], log=fx.work / "rekey.log")

        fx.cfg.salt = salt
        (fx.work / "install.cpio").write_bytes(usb.install_cpio(fx.cfg))

        b = fx.boot("rekeyed-decoy")
        self.addCleanup(b.logfile.close)
        self.addCleanup(b.close)
        b.expect("Password: ")
        b.sendline(NEW_DECOY)
        b.expect("PD-TARGET: marker=decoy", timeout=120)

        b = fx.boot("rekeyed-hidden")
        self.addCleanup(b.logfile.close)
        self.addCleanup(b.close)
        b.expect("Password: ")
        b.sendline("")
        b.expect("recovery shell")
        b.sendline(fx.sed)
        b.sendline("/early.sh")
        b.expect("Password: ")
        b.sendline(NEW_HIDDEN)
        b.expect("PD-TARGET: marker=hidden", timeout=120)

        b = fx.boot("old-password")
        self.addCleanup(b.logfile.close)
        self.addCleanup(b.close)
        b.expect("Password: ")
        b.sendline(fixture.DECOY_PW)
        b.expect("No bootable disk found")


if __name__ == "__main__":
    unittest.main()
