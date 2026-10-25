"""Boot a real NixOS initrd (with target/nixos/pd.nix) through pd and check
that it unlocks the disk and mounts root."""

import subprocess
import unittest

from tests import fixture, vm


def nixos_target():
    link = vm.REPO / ".nixos-test"
    subprocess.run([*vm.NIX, "build", "-j", "auto", "--out-link", str(link),
                    f"path:{vm.REPO}#nixosTest"], check=True)
    return link.resolve()


class NixOS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = nixos_target()
        cls.fx = fixture.make(kernel=t / "kernel", initrd=t / "initrd")

    @classmethod
    def tearDownClass(cls):
        vm.cleanup(cls.fx.work)

    def test_nixos_initrd_mounts_root(self):
        b = self.fx.boot("nixos", memory="3G")
        self.addCleanup(b.logfile.close)
        self.addCleanup(b.close)
        b.expect("Password: ")
        b.sendline(fixture.DECOY_PW)
        b.expect("Finished.*Unlock the pd disk", timeout=240)
        b.expect("Mounted.*/sysroot", timeout=120)


if __name__ == "__main__":
    unittest.main()
