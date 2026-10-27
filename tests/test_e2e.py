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

    def test_late_keyboard_gets_its_driver(self):
        # A USB keyboard plugged in after boot (as behind a slow hub or dock)
        # gets its driver from mdev's uevent handling.
        qmp = self.fx.work / "qmp.sock"
        b = self.boot("hotplug", extra_args=["-device", "qemu-xhci,id=xhci"], qmp=qmp)
        b.expect("Password: ")
        b.sendline("")
        b.expect("~ # ")
        b.sendline("grep -c usbhid /proc/modules")
        b.expect("~ # ")
        reply = b.qmp("device_add", driver="usb-kbd", bus="xhci.0", id="kbd")
        self.assertIn("return", reply, reply)
        b.sendline('for i in 1 2 3 4 5 6 7 8 9 10; do grep -q usbhid /proc/modules && break; sleep 0.5; done; '
                   'm=usbhid; grep -q $m /proc/modules && echo "LOADED-$m"')
        b.expect("LOADED-usbhid", timeout=30)

    def test_keymap(self):
        from pd import usb
        import dataclasses
        cfg = dataclasses.replace(self.fx.cfg, keymap="de")
        cpio = self.fx.work / "install-de.cpio"
        cpio.write_bytes(usb.install_cpio(cfg, self.fx.env))
        dumps = {}
        for name, initrd in [("us", self.fx.work / "install.cpio"), ("de", cpio)]:
            log = open(self.fx.work / f"boot-keymap-{name}.log", "w")
            self.addCleanup(log.close)
            b = vm.BootVM(self.fx.env, [self.fx.env / "boot-initrd.cpio.zst", initrd],
                          [self.fx.disk], log=log)
            self.addCleanup(b.close)
            b.expect("Password: ")
            b.sendline("")
            b.expect("~ # ")
            b.sendline('s=$(dumpkmap | md5sum | cut -c1-32); echo "KMAP<$s>"')
            b.expect(r"KMAP<([0-9a-f]{32})>")
            dumps[name] = b.child.match.group(1)
            b.close()
        self.assertNotEqual(dumps["us"], dumps["de"])

    def test_boot_environment_is_read_only(self):
        # With the disk attached read-only, the boot environment must still
        # get all the way to kexec: it never needs to write.
        b = self.boot("readonly", readonly_disks=True)
        b.expect("Password: ")
        b.sendline(fixture.DECOY_PW)
        b.expect("kexec_core: Starting new kernel", timeout=120)

    def test_disk_classes_and_order(self):
        """Each disk has its own salt.  Empty and small disks are never tried;
        a pd disk whose first sectors got a partition table still boots
        (tried last), since the salt at the end of the first MiB survives."""
        import os
        import subprocess
        work = self.fx.work
        blank = vm.sparse_disk(work / "blank.img", 4 << 30)
        small = work / "small.img"
        small.write_bytes(os.urandom(1 << 20))
        with open(small, "ab") as f:
            f.truncate(1 << 30)
        gpt = work / "gpt.img"
        subprocess.run(["cp", "--sparse=always", "--reflink=auto", str(self.fx.disk), str(gpt)], check=True)
        with open(gpt, "r+b") as f:
            f.seek(510)
            f.write(b"\x55\xaa" + b"EFI PART")
        log = open(work / "boot-order.log", "w")
        self.addCleanup(log.close)
        b = vm.BootVM(self.fx.env, [self.fx.env / "boot-initrd.cpio.zst", work / "install.cpio"],
                      [blank, small, gpt], log=log)
        self.addCleanup(b.close)
        b.expect("Password: ")
        b.sendline("")
        b.expect("~ # ")
        # The disks may enumerate in any order: match them by size.
        b.sendline('. /disk_class.sh; for d in vda vdb vdc; do '
                   'echo "CLASS-$(blockdev --getsize64 /dev/$d)=$(disk_class /dev/$d)."; done')
        classes = {}
        for _ in range(3):
            b.expect(r"CLASS-(\d+)=(\w+)\.")
            classes[int(b.child.match.group(1))] = b.child.match.group(2)
        self.assertEqual(classes, {4 << 30: "blank", 1 << 30: "small", gpt.stat().st_size: "used"})
        b.sendline("/early.sh")
        b.expect("Password: ")
        b.sendline(fixture.DECOY_PW)
        b.expect("PD-TARGET: marker=decoy", timeout=180)


if __name__ == "__main__":
    unittest.main()
