"""End-to-end: set up decoy and hidden sides in a tool VM, then boot each
through the boot environment into a fake target OS."""

import unittest

from tests import fixture, vm


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = fixture.make()

    @classmethod
    def tearDownClass(cls):
        vm.cleanup(cls.fx.work)

    def boot(self, name, **kwargs):
        b = self.fx.boot(name, **kwargs)
        self.addCleanup(b.logfile.close)
        self.addCleanup(b.close)
        return b

    def test_decoy_boot(self):
        b = self.boot("decoy")
        b.expect("Password: ")
        b.sendline(fixture.DECOY_PW)
        b.expect("PD-TARGET: hook ok", timeout=120)
        b.expect("PD-TARGET: boot_tmp removed")
        b.expect("PD-TARGET: marker=decoy")

    def test_hidden_boot(self):
        b = self.boot("hidden")
        b.expect("Password: ")
        b.sendline("")
        b.expect("recovery shell")
        b.sendline(self.fx.sed)
        b.sendline("/early.sh")
        b.expect("Password: ")
        b.sendline(fixture.HIDDEN_PW)
        b.expect("PD-TARGET: hook ok", timeout=120)
        b.expect("PD-TARGET: boot_tmp removed")
        b.expect("PD-TARGET: marker=hidden")

    def test_wrong_password_falls_to_shell(self):
        b = self.boot("wrong")
        b.expect("Password: ")
        b.sendline("not the password")
        b.expect("No bootable disk found")
        b.expect("recovery shell")

    def test_password_not_echoed(self):
        b = self.boot("echo")
        b.expect("Password: ")
        b.sendline("secret-canary-123")
        b.expect("recovery shell")
        self.assertNotIn("secret-canary-123", b.child.before)

    def test_boot_environment_is_read_only(self):
        # With the disk attached read-only, the boot environment must still
        # get all the way to kexec: it never needs to write.
        b = self.boot("readonly", readonly_disks=True)
        b.expect("Password: ")
        b.sendline(fixture.DECOY_PW)
        b.expect("kexec_core: Starting new kernel", timeout=120)


if __name__ == "__main__":
    unittest.main()
