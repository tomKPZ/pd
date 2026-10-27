"""Install Arch Linux on a pd disk (pacman from nixpkgs, packages from a
pinned archive.archlinux.org snapshot), with the pd mkinitcpio hook, and
boot it through pd.  Needs network access; downloads are cached in
~/.cache/pd-tests/pacman.  Skipped unless PD_TEST_ARCH=1."""

import os
import pathlib
import unittest

from pd import usb
from pd.config import Config
from tests import fixture, vm

SNAPSHOT = "2026/10/20"
PACKAGES = "base linux mkinitcpio lvm2 cryptsetup btrfs-progs"

CONFIG = """
disk = "/dev/vda"
vg = "sysvg"
swap_size = "64M"

[argon2]
t = 1
m_log2 = 12
p = 1

[[systems]]
name = "arch"
lv = "root"
kernel = "/boot/vmlinuz-linux"
initrd = "/boot/initramfs-linux.img"
cmdline = "console=ttyS0 root=/dev/sysvg/root rootflags=subvol=@ rw"
"""

INSTALL = r"""
set -eux
root=$PD_ROOT/arch
cat > /tmp/pacman.conf <<EOF
[options]
Architecture = x86_64
SigLevel = Never
CacheDir = /cache/pacman/
[core]
Server = https://archive.archlinux.org/repos/{snapshot}/\$repo/os/\$arch
[extra]
Server = https://archive.archlinux.org/repos/{snapshot}/\$repo/os/\$arch
EOF
mkdir -p /cache/pacman
pacstrap -C /tmp/pacman.conf -M -c "$root" {packages}
install -Dm755 /src/target/hook.sh "$root/usr/lib/pd/hook.sh"
install -Dm644 /src/target/arch/pd-install "$root/etc/initcpio/install/pd"
install -Dm644 /src/target/arch/pd-hook "$root/etc/initcpio/hooks/pd"
sed -i 's/^HOOKS=.*/HOOKS=(base udev autodetect microcode modconf kms keyboard keymap consolefont block pd lvm2 filesystems fsck)/' \
    "$root/etc/mkinitcpio.conf"
arch-chroot "$root" mkinitcpio -P
echo decoy > "$root/etc/pd-marker"
"""


@unittest.skipUnless(os.environ.get("PD_TEST_ARCH"), "set PD_TEST_ARCH=1 (needs network)")
class Arch(unittest.TestCase):
    def test_arch_boots(self):
        env = vm.testenv()
        work = vm.workdir()
        self.addCleanup(vm.cleanup, work)
        disk = vm.sparse_disk(work / "disk.img", 8 << 30)
        (work / "pd.toml").write_text(CONFIG)
        (work / "install.sh").write_text(INSTALL.format(snapshot=SNAPSHOT, packages=PACKAGES))
        cache = pathlib.Path.home() / ".cache" / "pd-tests"
        vm.ToolVM(env, work).run(
            f"printf '%s\\n' '{fixture.DECOY_PW}' | python3 -m pd init-decoy --config pd.toml "
            "--password-stdin --run 'bash /work/install.sh'",
            disks=[disk], network=True, cache=cache, timeout=3600, log=work / "install.log")

        cfg = Config.load(work / "pd.toml")
        (work / "install.cpio").write_bytes(usb.install_cpio(cfg))
        with open(work / "boot.log", "w") as log:
            b = vm.BootVM(env, [env / "boot-initrd.cpio.zst", work / "install.cpio"], [disk],
                          log=log)
            try:
                b.expect("Password: ")
                b.sendline(fixture.DECOY_PW)
                b.expect("Welcome to .*Arch Linux", timeout=300)
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main()

