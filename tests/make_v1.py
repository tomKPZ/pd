"""Build a v1-style pd disk inside the tool VM (for migration tests).

Reproduces v1's on-disk format (v1/pd.py): 512-byte Argon2 output, plain
boot volume keyed with bytes 0-63, a detached LUKS2 header in it keyed with
bytes 64-511, v1 tables written against /boot_tmp/disk, the hidden side at
[D-256MiB, D-1MiB) with data reversed over [0, D-256MiB), and the hidden
salt suffix "a".

Usage: make_v1.py DISK SALT DECOY_PW HIDDEN_PW KERNEL INITRD
"""

import os
import pathlib
import shutil
import subprocess
import sys

from pd import layout as L
from pd.util import Argon2Params, argon2, cmd, dm_device, mounted, mountpoint, plain_crypt, write_file

disk, salt, decoy_pw, hidden_pw, kernel, initrd = sys.argv[1:7]
PARAMS = Argon2Params(t=1, m_log2=12, p=1, length=512)
CRYPT = ["--cipher=aes-xts-plain64", "--key-size=512", "--sector-size=4096"]
META = 256 * L.MIB
D = L.disk_size(int(subprocess.check_output(["blockdev", "--getsize64", disk])))


def v1_reversed_table(end: int) -> str:
    # v1's reversed_table(0, end, ...)
    spc = L.REVERSE_CHUNK // 512
    n = end // 512 // spc
    total = end // 512
    return "".join(f"{i * spc} {spc} linear {{dev}} {total - (i + 1) * spc}\n" for i in range(n))


def make_side(pw: str, suffix: str, plain_off: int, data_table: str, marker: str):
    key = argon2(pw.encode(), salt + suffix, PARAMS)
    with plain_crypt("v1plain", disk, key[:64], CRYPT, offset=plain_off // 512,
                     size=(META - L.MIB) // 512) as pdev:
        cmd("mkfs.btrfs", "-q", "-f", "--nodiscard", "--data=dup", "--csum=blake2", pdev)
        with mountpoint("v1-") as mnt, mounted(pdev, mnt, "btrfs", "nodiscard"):
            write_file(mnt / "table.txt", data_table.format(dev="/boot_tmp/disk"))
            (mnt / "systems" / "fake").mkdir(parents=True)
            write_file(mnt / "systems" / "fake" / "env.sh",
                       "VG=sysvg\nLV_ROOT=root\nKERNEL=/boot/vmlinuz\nINITRD=/boot/initrd\n"
                       "CMDLINE='console=ttyS0 panic=-1 root=/dev/sysvg/root rootflags=subvol=@'\n")
            os.symlink("fake", mnt / "systems" / "default")
            write_file(mnt / "late.sh", "#!/usr/bin/bash\n# v1 late.sh placeholder\n", 0o755)
            hdr = mnt / "header.dat"
            vk = pathlib.Path("/run/v1vk")
            write_file(vk, os.urandom(64), 0o600)
            kf = pathlib.Path("/run/v1key")
            write_file(kf, key, 0o600)
            with dm_device("v1data", data_table.format(dev=disk)) as ddev:
                cmd("cryptsetup", "luksFormat", "--batch-mode", "--type=luks2", "--hash=sha512", *CRYPT,
                    f"--key-file={kf}", "--keyfile-offset=64", f"--header={hdr}",
                    f"--volume-key-file={vk}", "--pbkdf=argon2id", "--pbkdf-force-iterations=4",
                    "--pbkdf-memory=32", "--pbkdf-parallel=1", ddev)
                cmd("cryptsetup", "open", f"--key-file={kf}", "--keyfile-offset=64",
                    f"--header={hdr}", ddev, "v1luks")
                try:
                    lv = "/dev/mapper/v1luks"
                    nb = ["--config", "backup { backup = 0 archive = 0 }"]
                    cmd("lvm", "pvcreate", "-qq", lv, *nb)
                    cmd("lvm", "vgcreate", "-qq", "--physicalextentsize=4m", "sysvg", lv, *nb)
                    cmd("lvm", "lvcreate", "-qq", "--type=linear", "-L", "64m", "-n", "swap", "sysvg", *nb)
                    cmd("lvm", "lvcreate", "-qq", "--type=thin-pool", "--thinpool=thinpool",
                        "--chunksize=512k", "--poolmetadatasize=128m", "-L", "4m",
                        "--discards=nopassdown", "sysvg", *nb)
                    cmd("lvm", "lvextend", "-qq", "-l+100%FREE", "sysvg/thinpool", *nb)
                    cmd("lvm", "lvcreate", "-qq", "--thin", "-V", "4g", "-n", "root",
                        "sysvg/thinpool", *nb)
                    cmd("mkfs.btrfs", "-q", "-f", "--nodiscard", "/dev/sysvg/root")
                    with mountpoint("v1r-") as r:
                        with mounted("/dev/sysvg/root", r, "btrfs", "nodiscard"):
                            for sv in ("@", "@boot"):
                                cmd("btrfs", "subvolume", "create", r / sv, stdout=subprocess.DEVNULL)
                        with mounted("/dev/sysvg/root", r, "btrfs", "nodiscard,subvol=@"):
                            (r / "etc").mkdir()
                            write_file(r / "etc" / "pd-marker", marker + "\n")
                        with mounted("/dev/sysvg/root", r, "btrfs", "nodiscard,subvol=@boot"):
                            shutil.copy(kernel, r / "vmlinuz")
                            shutil.copy(initrd, r / "initrd")
                    cmd("lvm", "vgchange", "-an", "-qq", "sysvg", *nb)
                finally:
                    cmd("cryptsetup", "close", "v1luks")
            vk.unlink()
            kf.unlink()


# Randomize and zero the first MiB, as v1 did.
cmd("cryptsetup", "open", "--type=plain", "--batch-mode", *CRYPT, "--key-file=/dev/urandom",
    disk, "v1shred")
cmd("dd", "if=/dev/zero", "of=/dev/mapper/v1shred", "bs=4M", "status=none", check=False)
cmd("cryptsetup", "close", "v1shred")
cmd("dd", "if=/dev/zero", f"of={disk}", "bs=1M", "count=1", "conv=fsync", "status=none")

make_side(decoy_pw, "", L.MIB, f"0 {(D - META) // 512} linear {{dev}} {META // 512}\n", "decoy")
make_side(hidden_pw, "a", D - META, v1_reversed_table(D - META), "hidden")
print(f"V1-HIDDEN-OFFSET {(D - META) // 512}")

