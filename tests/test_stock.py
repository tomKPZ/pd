"""Stock boot (docs/stock-boot.md): a hidden side next to a stock decoy, booted
from a live system with the commands `pd init-hidden --stock-decoy` prints.

The decoy is built here with stock commands only: a randomized disk, a GPT
with an ESP and a LUKS2 partition, LVM with a thin pool inside.  Then:
* `pd init-hidden --stock-decoy` creates the hidden side without writing to
  the decoy (its LUKS header, LVM metadata and data are compared before and
  after);
* a fresh VM (standing in for the live system) types the printed commands:
  OpenSSL derives the key, cryptsetup opens the hidden boot volume, late.sh
  runs the frontier monitor against the LUKS decoy and kexecs into the
  hidden system.
"""

import dataclasses
import pathlib
import re
import unittest

from tests import fixture, vm

DECOY_PW = fixture.DECOY_PW
HIDDEN_PW = fixture.HIDDEN_PW

# The stock decoy.  Its LUKS2 volume uses 512-byte sectors (pd's own volumes
# use 4096), so the frontier monitor's copy of the decoy's settings matters.
STOCK_DECOY = f"""
cryptsetup open --type plain --cipher aes-xts-plain64 --key-size 512 --key-file /dev/urandom /dev/vda wipe
dd if=/dev/zero of=/dev/mapper/wipe bs=4M oflag=direct status=none || true
cryptsetup close wipe
sfdisk -q /dev/vda <<EOF
label: gpt
size=256MiB, type=U
type=L
EOF
mkfs.vfat /dev/vda1 >/dev/null
printf %s '{DECOY_PW}' | cryptsetup luksFormat -q --type luks2 --sector-size 512 \\
    --pbkdf pbkdf2 --pbkdf-force-iterations 1000 --key-file - /dev/vda2
printf %s '{DECOY_PW}' | cryptsetup open --key-file - /dev/vda2 cryptroot
lvm pvcreate -qq /dev/mapper/cryptroot
lvm vgcreate -qq vg0 /dev/mapper/cryptroot
lvm lvcreate -qq -L 128M -n swap vg0
lvm lvcreate -qq --type thin-pool -l 100%FREE -n pool vg0
lvm lvcreate -qq -V 6G -T vg0/pool -n root
mkfs.xfs -q /dev/vg0/root
mkdir -p /mnt/decoy
mount -o nodiscard /dev/vg0/root /mnt/decoy
echo decoy > /mnt/decoy/marker
head -c 300M /dev/urandom > /mnt/decoy/blob
umount /mnt/decoy
lvm vgchange -an -qq vg0
cryptsetup close cryptroot
"""

# Fingerprints of the decoy: its LUKS header area, the PV's LVM metadata (as
# decrypted), and its files.  (dm-thin can't run on a read-only mapping, so the
# test opens the decoy read-write, but only mounts it read-only.)
FINGERPRINT = f"""
fingerprint() {{
    echo "HEADER $(dd if=/dev/vda2 bs=1M count=16 status=none | sha256sum | cut -c1-16)"
    printf %s '{DECOY_PW}' | cryptsetup open --key-file - /dev/vda2 cryptroot
    echo "LVMMETA $(dd if=/dev/mapper/cryptroot bs=1M count=1 status=none | sha256sum | cut -c1-16)"
    lvm vgchange -ay -qq vg0
    mkdir -p /mnt/decoy
    mount -o ro,norecovery /dev/vg0/root /mnt/decoy
    echo "FILES $(cat /mnt/decoy/marker) $(sha256sum < /mnt/decoy/blob | cut -c1-16)"
    umount /mnt/decoy
    lvm vgchange -an -qq vg0
    cryptsetup close cryptroot
}}
"""


def fingerprints(out: str) -> list[tuple[str, str]]:
    return re.findall(r"^(HEADER|LVMMETA|FILES) ([^\r\n]*)", out, re.M)


@dataclasses.dataclass
class Stock:
    env: pathlib.Path
    work: pathlib.Path
    disk: pathlib.Path
    commands: list[str]  # what pd printed for the hidden boot
    offset: int


def make_stock(test: unittest.TestCase) -> Stock:
    """Build a stock decoy, add a hidden side with `pd init-hidden
    --stock-decoy`, and check that the decoy is unchanged."""
    env = vm.testenv()
    work = vm.workdir()
    test.addCleanup(vm.cleanup, work)
    disk = vm.sparse_disk(work / "disk.img", 8 << 30)
    (work / "install.sh").write_text(fixture.INSTALL.format(
        kernel=env / "bzImage", initrd=env / "faketarget-initrd.cpio.zst"))

    _, out = vm.ToolVM(env, work).run(STOCK_DECOY + FINGERPRINT + """
    fingerprint
    echo "UUID $(cryptsetup luksUUID /dev/vda2)"
    """, disks=[disk], log=work / "decoy.log")
    before = fingerprints(out)
    uuid = re.search(r"^UUID (\S+)", out, re.M).group(1)
    test.assertEqual([k for k, _ in before], ["HEADER", "LVMMETA", "FILES"])
    test.assertTrue(before[2][1].startswith("decoy "))

    # The salt is the decoy's LUKS UUID, read back at boot.
    (work / "pd.toml").write_text(fixture.config(uuid))
    _, out = vm.ToolVM(env, work).run(FINGERPRINT + f"""
    printf '%s\\n%s\\n' '{DECOY_PW}' '{HIDDEN_PW}' | python3 -m pd init-hidden --config pd.toml \\
        --stock-decoy /dev/vda2 --password-stdin --run 'sh /work/install.sh hidden' | tee hidden.txt
    fingerprint
    """, disks=[disk], log=work / "init-hidden.log")
    after = fingerprints(out)
    test.assertEqual(before, after, "pd changed the decoy")
    printed = (work / "hidden.txt").read_text()
    test.assertIn("The salt is the decoy's LUKS UUID", printed)
    after_header = printed.split("Hidden boot from a live system", 1)[1].splitlines()[1:]
    commands = [line[2:] for line in after_header if line.startswith("  ")]
    test.assertEqual(commands[0], "read -rs P")
    offset = int(re.search(r" -o (\d+) ", commands[1]).group(1))
    test.assertEqual(offset % 10**6, 0)
    return Stock(env=env, work=work, disk=disk, commands=commands, offset=offset)


class StockBoot(unittest.TestCase):
    def test_stock_decoy(self):
        st = make_stock(self)
        env, work, disk, commands, offset = st.env, st.work, st.disk, st.commands, st.offset

        # The live system: the password is set instead of read.
        script = f"P='{HIDDEN_PW}'\n" + "\n".join(commands[1:]) + "\n"
        _, out = vm.ToolVM(env, work).run(script, disks=[disk], check=False,
                                          log=work / "live-boot.log")
        self.assertRegex(out, r"Free space between the two sides: \d+ MiB")
        self.assertNotIn("Frontier check failed", out)
        self.assertNotIn("WARNING: decoy volume", out)
        self.assertNotIn("WARNING: less than", out)
        # A stock live system has gawk or mawk, not busybox awk.
        self.assertNotIn("awk:", out)
        self.assertIn("PD-TARGET: marker=hidden", out)

        # pd open reaches the hidden side the same way.
        _, out = vm.ToolVM(env, work).run(f"""
        printf '%s\\n' '{HIDDEN_PW}' | python3 -m pd open --config pd.toml --offset {offset} \\
            --password-stdin --run 'echo "OPENED $(cat fake/etc/pd-marker)"'
        """, disks=[disk], log=work / "open.log")
        self.assertIn("OPENED hidden", out)


if __name__ == "__main__":
    unittest.main()
