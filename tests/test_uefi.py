"""The release boot image: reproducible, records nothing about the build
host, and boots from a USB stick under OVMF with per-install files next to
it (systemd-stub's EFI/BOOT/BOOTX64.EFI.extra.d/*.cred).  The boot
environment removes the firmware's boot entry for it from NVRAM."""

import pathlib
import re
import shutil
import subprocess
import unittest

from pd import usb
from tests import fixture, vm


def make_esp(env: pathlib.Path, uki: pathlib.Path, out: pathlib.Path,
             extra: dict[str, bytes] | None = None) -> pathlib.Path:
    """A FAT image with the boot image, and `extra` files (name -> content)
    in EFI/BOOT/BOOTX64.EFI.extra.d/."""
    tools = env / "toolenv" / "bin"
    with open(out, "wb") as f:
        f.truncate(64 << 20)
    subprocess.run([tools / "mkfs.vfat", "-F", "32", str(out)], check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run([tools / "mmd", "-i", str(out), "::/EFI", "::/EFI/BOOT"], check=True)
    subprocess.run([tools / "mcopy", "-i", str(out), str(uki), "::/EFI/BOOT/BOOTX64.EFI"],
                   check=True)
    if extra:
        subprocess.run([tools / "mmd", "-i", str(out), "::/EFI/BOOT/BOOTX64.EFI.extra.d"], check=True)
        for name, data in extra.items():
            f = out.parent / name
            f.write_bytes(data)
            subprocess.run([tools / "mcopy", "-i", str(out), str(f),
                            f"::/EFI/BOOT/BOOTX64.EFI.extra.d/{name}"], check=True)
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
        release = fx.env / usb.IMAGE
        # Reproducible: building it again here gives the same bytes.
        rebuilt = fx.work / "rebuilt.efi"
        usb.build_uki(fx.env, rebuilt)
        self.assertEqual(rebuilt.read_bytes(), release.read_bytes(), "the boot image isn't reproducible")
        # It must not record the build host's OS.
        data = release.read_bytes()
        self.assertIn(b"NAME=Linux\nID=linux\n", data)
        host = pathlib.Path("/usr/lib/os-release")
        if host.exists():
            m = re.search(r'^PRETTY_NAME="?([^"\n]+)', host.read_text(), re.M)
            if m:
                self.assertNotIn(m.group(1).encode(), data)
        # The same image with a serial console, so the test can drive it.
        uki = fx.work / "BOOTX64.EFI"
        usb.build_uki(fx.env, uki, cmdline="console=ttyS0 " + usb.BOOT_CMDLINE)
        # The test config's Argon2 parameters aren't the defaults: without
        # argon2.cred next to the image, the decoy wouldn't open.
        extra = {f"{k}.cred": v for k, v in usb.credentials(fx.cfg).items()}
        self.assertIn("argon2.cred", extra)
        esp = make_esp(fx.env, uki, fx.work / "esp.img", extra)
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
        # The terminal echoes the command, so results are printed through
        # variables and matched in a form that doesn't appear literally in it.
        b.sendline(
            f"v=/sys/firmware/efi/efivars; cur=$(od -An -tx1 -j4 -N2 $v/BootCurrent-{g} | "
            "awk '{printf \"%s%s\", $2, $1}'); "
            f"e=gone; [ -e $v/Boot$(echo $cur | tr a-f A-F)-{g} ] && e=present; "
            f"o=$(od -An -tx1 -j4 -v $v/BootOrder-{g} | tr -s ' \\n' ' '); "
            "lo=${cur#??}; hi=${cur%??}; "
            "case \" $o \" in *\" $lo $hi \"*) r=present ;; *) r=gone ;; esac; "
            "echo \"NVRAM: entry_$e order_$r.\"")
        b.expect(r"NVRAM: entry_(\w+) order_(\w+)\.")
        self.assertEqual(b.child.match.group(1), "gone")
        self.assertEqual(b.child.match.group(2), "gone")
        # systemd-stub passed the file in; with it, the decoy boots.
        b.sendline("test -s /.extra/credentials/argon2.cred && echo \"CRED: $((40 + 2)).\"")
        b.expect(r"CRED: 42\.")
        b.sendline("/early.sh")
        b.expect("Password: ")
        b.sendline(fixture.DECOY_PW)
        b.expect("PD-TARGET: marker=decoy", timeout=180)


if __name__ == "__main__":
    unittest.main()

