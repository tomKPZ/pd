"""Run setup the way it's done on real hardware: boot the RAM-only setup
environment with the target disk attached, run pd in its shell, then boot
the disk through pd."""

import secrets
import subprocess
import tarfile
import unittest

from pd import usb
from pd.config import Config
from tests import fixture, vm


class SetupEnv(unittest.TestCase):
    def test_setup_then_boot(self):
        env = vm.testenv()
        work = vm.workdir()
        self.addCleanup(vm.cleanup, work)
        disk = vm.sparse_disk(work / "disk.img", 8 << 30)
        config = fixture.CONFIG.format(salt=secrets.token_hex(32))
        (work / "pd.toml").write_text(config)

        # A second disk carries the config and the fake OS to install, as a
        # tar archive written straight onto the device.
        with tarfile.open(work / "payload.tar", "w", format=tarfile.USTAR_FORMAT,
                          dereference=True) as tar:
            tar.add(work / "pd.toml", "pd.toml")
            tar.add(env / "bzImage", "vmlinuz")
            tar.add(env / "faketarget-initrd.cpio.zst", "initrd")
        payload = work / "payload.tar"
        with open(payload, "ab") as f:
            f.truncate((payload.stat().st_size + (1 << 20)) // (1 << 20) * (1 << 20))

        log = open(work / "setup-env.log", "w")
        self.addCleanup(log.close)
        b = vm.BootVM(env, [env / "setup-initrd.cpio.zst"], [disk, payload], memory="4G", log=log)
        self.addCleanup(b.close)
        b.expect("pd setup environment", timeout=240)
        prompt = r"[#$] $"
        b.expect(prompt)

        def run(command, timeout=600):
            b.sendline(command + '; echo "rc=$?."')
            b.expect(r"rc=(\d+)\.", timeout=timeout)
            self.assertEqual(b.child.match.group(1), "0", command)
            b.expect(prompt)

        run("mkdir payload && cd payload && tar -xf /dev/vdb && cd ..")
        run("pd randomize --config payload/pd.toml")
        install = ("mkdir -p $PD_ROOT/fake/etc && cp /root/payload/vmlinuz $PD_ROOT/fake/boot/vmlinuz && "
                   "cp /root/payload/initrd $PD_ROOT/fake/boot/initrd && "
                   "echo decoy > $PD_ROOT/fake/etc/pd-marker")
        run(f"printf '%s\\n' '{fixture.DECOY_PW}' | pd init-decoy --config payload/pd.toml "
            f"--password-stdin --run '{install}'")
        b.sendline("sync; poweroff -f")
        b.child.expect(vm.pexpect.EOF, timeout=60)

        cfg = Config.load(work / "pd.toml")
        (work / "install.cpio").write_bytes(usb.install_cpio(cfg))
        with open(work / "boot.log", "w") as blog:
            b2 = vm.BootVM(env, [env / "boot-initrd.cpio.zst", work / "install.cpio"], [disk],
                           log=blog)
            try:
                b2.expect("Password: ")
                b2.sendline(fixture.DECOY_PW)
                b2.expect("PD-TARGET: marker=decoy", timeout=180)
            finally:
                b2.close()


if __name__ == "__main__":
    unittest.main()
