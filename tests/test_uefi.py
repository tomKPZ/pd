"""Boot the real UKI from a USB stick under OVMF, and check that the boot
environment removes the firmware's boot entry for it from NVRAM."""

import pathlib
import shutil
import subprocess
import unittest

from pd import usb
from tests import fixture, vm


def make_esp(env: pathlib.Path, uki: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    tools = env / "toolenv" / "bin"
    with open(out, "wb") as f:
        f.truncate(64 << 20)
    subprocess.run([tools / "mkfs.vfat", "-F", "32", str(out)], check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run([tools / "mmd", "-i", str(out), "::/EFI", "::/EFI/BOOT"], check=True)
    subprocess.run([tools / "mcopy", "-i", str(out), str(uki), "::/EFI/BOOT/BOOTX64.EFI"],
                   check=True)
    return out


class Uefi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = fixture.make()

    @classmethod
    def tearDownClass(cls):
        vm.cleanup(cls.fx.work)

    def test_uki_boot_removes_nvram_entry(self):
        fx = self.fx
        uki = fx.work / "BOOTX64.EFI"
        usb.build_uki(fx.cfg, fx.env, uki, cmdline="console=ttyS0 " + usb.BOOT_CMDLINE)
        esp = make_esp(fx.env, uki, fx.work / "esp.img")
        ovmf = fx.env / "ovmf" / "FV"
        vars_ = fx.work / "OVMF_VARS.fd"
        shutil.copy(ovmf / "OVMF_VARS.fd", vars_)
        vars_.chmod(0o644)
        log = open(fx.work / "boot-uefi.log", "w")
        self.addCleanup(log.close)
        b = vm.BootVM(fx.env, [], [fx.disk], log=log, uefi={
            "code": ovmf / "OVMF_CODE.fd", "vars": vars_, "esp": esp})
        self.addCleanup(b.close)
        b.expect("Password: ", timeout=240)
        b.sendline("wrong")
        b.expect("recovery shell")
        b.expect("~ # ")
        g = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
        b.sendline(
            f"v=/sys/firmware/efi/efivars; cur=$(od -An -tx1 -j4 -N2 $v/BootCurrent-{g} | "
            "awk '{printf \"%s%s\", toupper($2), toupper($1)}'); "
            f"echo CUR=$cur; [ -e $v/Boot$cur-{g} ] && echo ENTRY-PRESENT || echo ENTRY-GONE; "
            f"od -An -tx1 -j4 -v $v/BootOrder-{g} | tr -s ' \\n' ' '; echo ORDER-END")
        b.expect("ENTRY-(GONE|PRESENT)")
        self.assertEqual(b.child.match.group(0), "ENTRY-GONE")
        b.expect("ORDER-END")
        cur = b.child.before
        # Driving the real boot path proves the UKI works; the decoy itself is
        # covered by test_e2e.


if __name__ == "__main__":
    unittest.main()

