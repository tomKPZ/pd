"""pd rekey: change both passwords and the Argon2 parameters; pd header: a new
salt on the disk.  Then boot both sides with a USB written for them."""

import re
import unittest

from pd import header, usb
from pd.util import Argon2Params
from tests import fixture, vm

NEW_DECOY = "new decoy password"
NEW_HIDDEN = "new hidden password"


class Rekey(unittest.TestCase):
    def test_rekey_both_sides_new_argon2_and_header(self):
        fx = fixture.make()
        self.addCleanup(vm.cleanup, fx.work)
        offset = fixture.hidden_offset_sectors(fx)
        old_salt = header.disk_salt(str(fx.disk))
        vm.ToolVM(fx.env, fx.work).run(f"""
        printf '%s\\n%s\\n' '{fixture.DECOY_PW}' '{NEW_DECOY}' | python3 -m pd rekey --config pd.toml --new-argon2 2,13,2 --password-stdin
        printf '%s\\n%s\\n' '{fixture.HIDDEN_PW}' '{NEW_HIDDEN}' | python3 -m pd rekey --config pd.toml --new-argon2 2,13,2 --offset {offset} --password-stdin
        sed -i 's/^m_log2 = 12/m_log2 = 13/; s/^t = 1/t = 2/; s/^p = 1/p = 2/' pd.toml
        printf '%s\\n' '{fixture.DECOY_PW}' | python3 -m pd rekey --config pd.toml --new-salt 0123456789abcdef --password-stdin \\
            || echo NEW-SALT-REFUSED
        printf '%s\\n%s\\n' '{NEW_DECOY}' '{NEW_HIDDEN}' | python3 -m pd header --config pd.toml --hidden-offset {offset} --password-stdin
        """, disks=[fx.disk], log=fx.work / "rekey.log")
        log = (fx.work / "rekey.log").read_text()
        self.assertIn("NEW-SALT-REFUSED", log)
        self.assertIn("re-keying the side at sector 2048", log)
        self.assertNotEqual(header.disk_salt(str(fx.disk)), old_salt)
        self.assertFalse(header.pending_path(str(fx.work / "pd.toml")).exists())

        fx.cfg.argon2 = Argon2Params(t=2, m_log2=13, p=2)
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
