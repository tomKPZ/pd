"""End-to-end: set up decoy and hidden sides in a tool VM, then boot each
through the boot environment into a fake target OS."""

import os
import pathlib
import re
import secrets
import sys
import unittest

from pd import usb
from pd.config import Config
from tests import vm

DECOY_PW = "decoy password"
HIDDEN_PW = "hidden password"
DISK_SIZE = 8 << 30

CONFIG = """
disk = "/dev/vda"
salt = "{salt}"
vg = "sysvg"
swap_size = "64M"
hidden_cap = 0.4

[argon2]
t = 1
m_log2 = 12
p = 1

[[systems]]
name = "fake"
lv = "root"
kernel = "/boot/vmlinuz"
initrd = "/boot/initrd"
cmdline = "console=ttyS0 panic=-1 root=/dev/sysvg/root rootflags=subvol=@"
"""

INSTALL = """
set -eu
side=$1
root=$PD_ROOT/fake
cp {env}/bzImage $root/boot/vmlinuz
cp {env}/faketarget-initrd.cpio.zst $root/boot/initrd
mkdir -p $root/etc
echo "$side" > $root/etc/pd-marker
"""


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = vm.testenv()
        cls.work = vm.workdir()
        cls.disk = vm.sparse_disk(cls.work / "disk.img", DISK_SIZE)
        (cls.work / "pd.toml").write_text(CONFIG.format(salt=secrets.token_hex(32)))
        (cls.work / "install.sh").write_text(INSTALL.format(env=cls.env))
        cls.cfg = Config.load(cls.work / "pd.toml")
        script = f"""
        python3 -m pd randomize --config pd.toml
        printf '%s\\n' '{DECOY_PW}' | python3 -m pd init-decoy --config pd.toml --password-stdin --run 'sh /work/install.sh decoy'
        printf '%s\\n%s\\n' '{DECOY_PW}' '{HIDDEN_PW}' | python3 -m pd init-hidden --config pd.toml --password-stdin --run 'sh /work/install.sh hidden' | tee hidden.txt
        """
        vm.ToolVM(cls.env, cls.work).run(script, disks=[cls.disk], timeout=900,
                                         log=cls.work / "setup.log")
        out = (cls.work / "hidden.txt").read_text()
        cls.sed = re.search(r"(sed -i s/2048/\d+/ /early_env.sh)", out).group(1)
        (cls.work / "install.cpio").write_bytes(usb.install_cpio(cls.cfg))

    @classmethod
    def tearDownClass(cls):
        vm.cleanup(cls.work)

    def boot(self, name):
        log = open(self.work / f"boot-{name}.log", "w")
        b = vm.BootVM(self.env, [self.env / "boot-initrd.cpio.zst", self.work / "install.cpio"],
                      [self.disk], log=log)
        self.addCleanup(b.close)
        self.addCleanup(log.close)
        return b

    def test_decoy_boot(self):
        b = self.boot("decoy")
        b.expect("Password: ")
        b.sendline(DECOY_PW)
        b.expect("PD-TARGET: hook ok", timeout=120)
        b.expect("PD-TARGET: boot_tmp removed")
        b.expect("PD-TARGET: marker=decoy")

    def test_hidden_boot(self):
        b = self.boot("hidden")
        b.expect("Password: ")
        b.sendline("")
        b.expect("recovery shell")
        b.sendline(self.sed)
        b.sendline("/early.sh")
        b.expect("Password: ")
        b.sendline(HIDDEN_PW)
        b.expect("PD-TARGET: hook ok", timeout=120)
        b.expect("PD-TARGET: boot_tmp removed")
        b.expect("PD-TARGET: marker=hidden")

    def test_wrong_password_falls_to_shell(self):
        b = self.boot("wrong")
        b.expect("Password: ")
        b.sendline("not the password")
        b.expect("No bootable disk found")
        b.expect("recovery shell")


if __name__ == "__main__":
    unittest.main()

