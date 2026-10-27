"""Unit tests: the boot USB's per-install files (pd/usb.py) and the disk's
edges and salt (pd/header.py, pd/disk_class.sh, run on image files)."""

import os
import pathlib
import tempfile
import unittest

from pd import header, usb
from pd import layout as L
from pd.config import Config
from pd.util import Argon2Params


def old_cpio(files: dict[str, bytes]) -> bytes:
    """The per-install cpio of boot images from before the salt moved to the
    disk (salt.txt at the top level)."""
    out = b""
    for i, (name, data) in enumerate(files.items(), start=1):
        out += usb._cpio_entry(name, data, 0o100644, i)
    return out + usb._cpio_entry("TRAILER!!!", b"", 0, 0)


class UsbTest(unittest.TestCase):
    def test_old_salt_round_trip(self):
        blob = b"\x28\xb5\x2f\xfd" + bytes(1001) + old_cpio({"salt.txt": b"ab" * 32}) + bytes(77)
        with tempfile.NamedTemporaryFile() as f:
            f.write(blob)
            f.flush()
            self.assertEqual(usb.read_salt(pathlib.Path(f.name)), "ab" * 32)

    def test_early_env_has_one_2048(self):
        env = usb.early_env()
        self.assertEqual(env.count("2048"), 1)
        self.assertIn("PLAIN_OFFSET=2048\n", env)
        self.assertIn(Argon2Params().shell(), env)

    def test_credentials(self):
        # Defaults and the disk salt: nothing next to the image.
        self.assertEqual(usb.credentials(Config(disk="/dev/null")), {})
        cfg = Config(disk="/dev/null", salt="0123456789abcdef", argon2=Argon2Params(t=1, m_log2=12, p=1))
        creds = usb.credentials(cfg)
        self.assertEqual(creds["salt"], b"0123456789abcdef")
        self.assertEqual(creds["argon2"], b"-id -t 1 -m 12 -p 1 -l 64 -v 13 -r")
        cpio = usb.install_cpio(cfg)
        for name in (b".extra\0", b".extra/credentials\0", b".extra/credentials/argon2.cred\0",
                     b".extra/credentials/salt.cred\0"):
            self.assertIn(name, cpio)

    def test_write_usb(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            image = d / "image.efi"
            image.write_bytes(b"MZ boot image")
            esp = d / "esp"
            cfg = Config(disk="/dev/null", argon2=Argon2Params(t=2))
            usb.write_usb(cfg, image, esp)
            boot = esp / "EFI" / "BOOT"
            self.assertEqual((boot / "BOOTX64.EFI").read_bytes(), b"MZ boot image")
            self.assertEqual(sorted(p.name for p in (boot / "BOOTX64.EFI.extra.d").iterdir()),
                             ["argon2.cred"])
            # Without a config, the stick is just the image.
            usb.write_usb(None, image, esp)
            self.assertFalse((boot / "BOOTX64.EFI.extra.d").exists())


class EdgesTest(unittest.TestCase):
    """pd/disk_class.sh on image files (no kernel partitions to see)."""

    def image(self, head: bytes, size: int = 3 << 30) -> pathlib.Path:
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".img")
        self.addCleanup(os.unlink, f.name)
        f.write(head)
        f.truncate(size)
        f.close()
        return pathlib.Path(f.name)

    def random_head(self) -> bytearray:
        while True:
            head = bytearray(os.urandom(L.HEADER))
            if head[510:512] != b"\x55\xaa":
                return head

    def test_classes(self):
        head = self.random_head()
        plain = self.image(bytes(head))
        self.assertEqual(header.disk_class(str(plain)), "plain")
        self.assertEqual(header.disk_salt(str(plain)), bytes(head[L.SALT_OFFSET:]).hex())
        self.assertEqual(header.disk_class(str(self.image(b""))), "blank")
        self.assertEqual(header.disk_class(str(self.image(bytes(head), 1 << 30))), "small")
        mbr = bytearray(head)
        mbr[510:512] = b"\x55\xaa"
        self.assertEqual(header.disk_class(str(self.image(bytes(mbr)))), "used")
        gpt = bytearray(head)
        gpt[4096:4104] = b"EFI PART"
        self.assertEqual(header.disk_class(str(self.image(bytes(gpt)))), "used")
        ff = bytearray(head)
        ff[L.SALT_OFFSET:] = b"\xff" * L.SALT_LEN
        self.assertEqual(header.disk_class(str(self.image(bytes(ff)))), "blank")

    def test_filesystem_is_used(self):
        # An ext4 superblock (magic 0xef53 at 1024 + 56) is recognized by blkid.
        head = self.random_head()
        sb = bytearray(1024)
        sb[0:4] = (1 << 16).to_bytes(4, "little")        # s_inodes_count
        sb[4:8] = (1 << 18).to_bytes(4, "little")        # s_blocks_count
        sb[24:28] = (2).to_bytes(4, "little")            # s_log_block_size: 4 KiB
        sb[32:36] = (32768).to_bytes(4, "little")        # s_blocks_per_group
        sb[40:44] = (8192).to_bytes(4, "little")         # s_inodes_per_group
        sb[56:58] = b"\x53\xef"                          # s_magic
        sb[58:60] = (1).to_bytes(2, "little")            # s_state: clean
        sb[76:80] = (1).to_bytes(4, "little")            # s_rev_level: dynamic
        sb[88:90] = (256).to_bytes(2, "little")          # s_inode_size
        head[1024:2048] = sb
        img = self.image(bytes(head))
        if not header.signatures(str(img)):
            self.skipTest("this host's libblkid doesn't recognize the hand-made superblock")
        self.assertEqual(header.disk_class(str(img)), "used")

    def test_salt_blank(self):
        self.assertTrue(header.salt_blank("00" * 32))
        self.assertTrue(header.salt_blank("ab" * 32))
        self.assertTrue(header.salt_blank("ab" * 31))
        self.assertFalse(header.salt_blank("ab" * 31 + "ac"))
        self.assertFalse(header.salt_blank("0a" + "a0" * 31))


if __name__ == "__main__":
    unittest.main()
