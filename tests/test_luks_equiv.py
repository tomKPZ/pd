"""Migration groundwork (docs/migration.md): a v1 data region (LUKS2 with a
detached header, data offset 0) must read identically through plain
dm-crypt keyed with the LUKS volume key.  If so, v1 installs migrate
without re-encrypting their data."""

import unittest

from tests import vm

SCRIPT = """
head -c 64 /dev/urandom > /tmp/vk
head -c 64 /dev/urandom > /tmp/pw
cryptsetup luksFormat --batch-mode --type luks2 --hash=sha512 --cipher=aes-xts-plain64 \\
    --key-size=512 --sector-size=4096 --header=/tmp/header --volume-key-file=/tmp/vk \\
    --key-file=/tmp/pw --pbkdf=argon2id --pbkdf-force-iterations=4 --pbkdf-memory=32 \\
    --pbkdf-parallel=1 /dev/vda
cryptsetup open --header=/tmp/header --key-file=/tmp/pw /dev/vda luks
head -c 256M /dev/urandom > /tmp/data
dd if=/tmp/data of=/dev/mapper/luks bs=4M oflag=direct status=none
cryptsetup close luks
# Extract the volume key the way the migration tool will.
cryptsetup luksDump --dump-volume-key --volume-key-file=/tmp/vk2 --batch-mode \\
    --header=/tmp/header --key-file=/tmp/pw /dev/vda >/dev/null
cmp /tmp/vk /tmp/vk2 && echo VK-MATCH
cryptsetup open --type plain --readonly --batch-mode --cipher=aes-xts-plain64 --key-size=512 \\
    --sector-size=4096 --key-file=/tmp/vk2 /dev/vda plain
cmp <(head -c 256M /dev/mapper/plain) /tmp/data && echo DATA-MATCH
cryptsetup close plain
"""


class LuksPlainEquivalence(unittest.TestCase):
    def test_equivalent(self):
        env = vm.testenv()
        work = vm.workdir()
        try:
            disk = vm.sparse_disk(work / "disk.img", 1 << 30)
            _, out = vm.ToolVM(env, work).run(SCRIPT, disks=[disk], timeout=300)
        finally:
            vm.cleanup(work)
        self.assertIn("VK-MATCH", out)
        self.assertIn("DATA-MATCH", out)


if __name__ == "__main__":
    unittest.main()

