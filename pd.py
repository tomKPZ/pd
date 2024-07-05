#!/usr/bin/env python3

import base64
import contextlib
import functools
import math
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB
INITRD_GZ_LEVEL = "1"
DISK_SIZE = 1 * GIB
SWAP_SIZE_PE = 64 // 4
PASSWORD = "test"
META_SIZE = 64 * MIB
MOVE_PES = False
DMSETUP_SECTOR_SIZE = 512
REVERSE_CHUNK_SIZE = 8 * MIB
ZERO_HEADER = 4 * MIB

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def cmd(*args, check=True, **kwargs):
    return subprocess.run(args, check=check, **kwargs)


def cmd_output(*args, **kwargs):
    return cmd(*args, stdout=subprocess.PIPE, **kwargs).stdout.decode().strip()


def add_file(root, file):
    dst = os.path.join(root, file.lstrip("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy(file, dst)


def add_library(root, lib):
    dst = os.path.join(root, lib.lstrip("/"))
    if not os.path.isfile(dst):
        add_file(root, lib)
        subprocess.run(["strip", "--strip-unneeded", dst], check=True)


def add_binary(root, bin):
    add_file(root, bin)
    dst = os.path.join(root, bin.lstrip("/"))
    subprocess.run(["strip", "--strip-all", dst], check=True)

    links = subprocess.run(
        ["find", "-L", os.path.dirname(bin), "-samefile", bin, "-xtype", "l"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    for link in links:
        link_path = os.path.join(root, link.lstrip("/"))
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        os.symlink(os.path.basename(bin), link_path)

    dependencies = subprocess.run(
        ["ldd", bin], capture_output=True, text=True, check=False
    ).stdout.splitlines()

    for dep in dependencies:
        dep_path = (
            dep.split()[2]
            if len(dep.split()) > 2 and dep.split()[2].startswith("/")
            else None
        )
        if dep_path:
            add_library(root, dep_path)


def common_initrd(root):
    os.makedirs(os.path.join(root, "dev"), exist_ok=True)
    os.makedirs(os.path.join(root, "etc"), exist_ok=True)
    os.makedirs(os.path.join(root, "mnt"), exist_ok=True)
    os.makedirs(os.path.join(root, "proc"), exist_ok=True)
    os.makedirs(os.path.join(root, "run"), exist_ok=True)
    os.makedirs(os.path.join(root, "sys"), exist_ok=True)
    os.makedirs(os.path.join(root, "tmp"), exist_ok=True)
    os.makedirs(os.path.join(root, "usr", "bin"), exist_ok=True)
    os.makedirs(os.path.join(root, "usr", "lib"), exist_ok=True)
    os.makedirs(os.path.join(root, "var"), exist_ok=True)
    os.symlink("usr/lib", os.path.join(root, "lib"))
    os.symlink("bin", os.path.join(root, "usr", "sbin"))
    os.symlink("usr/bin", os.path.join(root, "bin"))
    os.symlink("usr/bin", os.path.join(root, "sbin"))
    os.symlink("../run", os.path.join(root, "var", "run"))
    os.symlink("lib", os.path.join(root, "usr", "lib64"))
    os.symlink("usr/lib", os.path.join(root, "lib64"))

    bins_to_add = [
        "/usr/bin/argon2",
        "/usr/bin/cryptsetup",
        "/usr/bin/dmsetup",
        "/usr/bin/integritysetup",
        "/usr/bin/kexec",
        "/usr/bin/lvm",
    ]

    for bin in bins_to_add:
        add_binary(root, bin)

    add_library(root, "/usr/lib/ossl-modules/legacy.so")

    add_binary(root, "/usr/bin/busybox")
    applets = subprocess.run(
        ["/usr/bin/busybox", "--list"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for applet in applets:
        if applet != "busybox":
            os.symlink("busybox", os.path.join(root, "usr", "bin", applet))

    os.symlink("../proc/self/mounts", os.path.join(root, "etc", "mtab"))
    open(os.path.join(root, "etc", "fstab"), "w").close()
    open(os.path.join(root, "etc", "ld.so.conf"), "w").close()

    os.makedirs(os.path.join(root, "etc", "lvm"), exist_ok=True)
    open(os.path.join(root, "etc", "lvm", "lvm.conf"), "w").close()

    subprocess.run(["ldconfig", "-r", root], check=True)

    shutil.copy(os.path.join(SCRIPT_DIR, "init"), root)


def bootstrap_initrd(out_file, salt: str, hidden):
    with tempfile.TemporaryDirectory() as root:
        common_initrd(root)

        with open(os.path.join(root, "salt.txt"), "w") as salt_file:
            salt_file.write(salt)
        shutil.copy(os.path.join(SCRIPT_DIR, "early.sh"), root)
        with open(os.path.join(root, "table.txt"), "w") as table:
            print(
                (
                    dmsetup_table(DISK_SIZE - META_SIZE, DISK_SIZE, "/dev/disk")
                    if hidden
                    else dmsetup_table(ZERO_HEADER, META_SIZE, "/dev/disk")
                ),
                file=table,
            )

        subprocess.run(
            f"find . -print0 | cpio --null -o --format=newc | gzip -{INITRD_GZ_LEVEL} >{out_file}",
            cwd=root,
            shell=True,
            check=True,
        )


def system_initrd(out_file):
    with tempfile.TemporaryDirectory() as root:
        common_initrd(root)

        shutil.copy(
            os.path.join(SCRIPT_DIR, "system.sh"), os.path.join(root, "early.sh")
        )

        subprocess.run(
            f"find . -print0 | cpio --null -o --format=newc | gzip -{INITRD_GZ_LEVEL} >{out_file}",
            cwd=root,
            shell=True,
            check=True,
        )


def dd(**kwargs):
    DD_OPS = {
        "bs",
        "cbs",
        "conv",
        "count",
        "ibs",
        "if",
        "iflag",
        "obs",
        "of",
        "oflag",
        "seek",
        "skip",
        "status",
    }
    dd = ["dd"]
    forward_kwargs = {}
    for k, v in kwargs.items():
        k = k.lstrip("_")
        if k in DD_OPS:
            dd.append(f"{k}={v}")
        else:
            forward_kwargs[k] = v
    return cmd(*dd, **forward_kwargs)


@contextlib.contextmanager
def loopback_device(file_path):
    loop_dev = cmd_output("losetup", "-f", "--show", file_path)
    try:
        yield loop_dev
    finally:
        cmd("losetup", "-d", loop_dev)


@contextlib.contextmanager
def temp_file(suffix=""):
    file = tempfile.mktemp(suffix=suffix)
    try:
        yield file
    finally:
        os.remove(file)


def dmsetup_table(start: int, end: int, wrap_dev: str):
    start_sector, start_rem = divmod(start, DMSETUP_SECTOR_SIZE)
    end_sector, end_rem = divmod(end, DMSETUP_SECTOR_SIZE)
    assert start_rem == 0 and end_rem == 0
    return f"0 {end_sector-start_sector} linear {wrap_dev} {start_sector}"


def reversed_table(start: int, end: int, wrap_dev: str):
    sectors_per_chunk = REVERSE_CHUNK_SIZE // DMSETUP_SECTOR_SIZE

    start_sector, start_rem = divmod(start, DMSETUP_SECTOR_SIZE)
    end_sector, end_rem = divmod(end, DMSETUP_SECTOR_SIZE)
    assert start_rem == 0 and end_rem == 0

    num_sectors = end_sector - start_sector
    num_chunks = num_sectors // sectors_per_chunk

    dmsetup_input = []
    for i in reversed(range(num_chunks)):
        start = i * sectors_per_chunk
        reversed_start = num_sectors - (i + 1) * sectors_per_chunk
        dmsetup_input.append(
            f"{reversed_start} {sectors_per_chunk} linear {wrap_dev} {start_sector + start}"
        )
    return "\n".join(dmsetup_input) + "\n"


@contextlib.contextmanager
def device_view(table: str):
    device_name = f"view_{str(uuid.uuid4()).replace('-', '_')}"
    device_path = f"/dev/mapper/{device_name}"

    cmd(
        "dmsetup",
        "create",
        os.path.basename(device_path),
        input=table.encode(),
    )
    try:
        yield device_path
    finally:
        cmd("dmsetup", "remove", device_path)


@contextlib.contextmanager
def plain_dmcrypt(key_file, wrap_dev):
    crypt_name = f"dmcrypt_test_{str(uuid.uuid4()).replace('-', '_')}"
    crypt_dev = f"/dev/mapper/{crypt_name}"
    cmd(
        "cryptsetup",
        "open",
        "--type=plain",
        "--cipher=aes-xts-plain64",
        "--key-size=512",
        f"--key-file={key_file}",
        "--sector-size=4096",
        wrap_dev,
        crypt_name,
    )
    try:
        yield crypt_dev
    finally:
        cmd("cryptsetup", "close", crypt_name)


@contextlib.contextmanager
def detached_luks(key_file, key_file_offset, header_file, wrap_dev):
    luks_name = f"luks_test_{str(uuid.uuid4()).replace('-', '_')}"
    luks_dev = f"/dev/mapper/{luks_name}"

    cmd(
        "cryptsetup",
        "luksFormat",
        "--use-random",
        "--batch-mode",
        "--hash=sha512",
        "--cipher=aes-xts-plain64",
        "--key-size=512",
        "--sector-size=4096",
        f"--key-file={key_file}",
        f"--keyfile-offset={key_file_offset}",
        f"--header={header_file}",
        # The password has already passed through a KDF, but LUKS doesn't
        # support raw key files, so set the PBKDF parameters to the minimum.
        "--pbkdf=argon2id",
        "--pbkdf-force-iterations=4",
        "--pbkdf-memory=32",
        "--pbkdf-parallel=1",
        wrap_dev,
    )
    cmd(
        "cryptsetup",
        "open",
        f"--key-file={key_file}",
        f"--keyfile-offset={key_file_offset}",
        f"--header={header_file}",
        wrap_dev,
        luks_name,
    )
    try:
        yield luks_dev
    finally:
        cmd("cryptsetup", "close", luks_name)


@contextlib.contextmanager
def integrity(wrap_dev):
    integrity_name = f"integrity_test_{str(uuid.uuid4()).replace('-', '_')}"
    integrity_dev = f"/dev/mapper/{integrity_name}"

    cmd(
        "integritysetup",
        "format",
        "--integrity=sha512",
        "--batch-mode",
        "--integrity-no-journal",
        "--sector-size=4096",
        wrap_dev,
    )
    cmd("integritysetup", "open", wrap_dev, integrity_name)
    try:
        dd(_if="/dev/zero", of=integrity_dev, bs="4M", check=False)
        yield integrity_dev
    finally:
        cmd("integritysetup", "close", integrity_name)


@contextlib.contextmanager
def lvm(wrap_dev):
    lv_name = "test_lv"
    vg_name = f"system_{str(uuid.uuid4()).replace('-', '_')}"
    swap_name = "swap"
    swap_dev = f"/dev/{vg_name}/{swap_name}"
    thinpool_name = "thinpool"
    thinpool_dev = f"/dev/{vg_name}/{thinpool_name}"
    lv_dev = f"/dev/{vg_name}/{lv_name}"

    dd(_if="/dev/zero", of=wrap_dev, bs="4M", count=1)
    cmd("pvcreate", wrap_dev)
    cmd(
        "vgcreate",
        "--physicalextentsize=4m",
        vg_name,
        wrap_dev,
    )
    try:
        cmd(
            "lvcreate",
            "--type=linear",
            f"--size={4 * SWAP_SIZE_PE}m",
            f"--name={swap_name}",
            vg_name,
        )
        dd(_if="/dev/zero", of=swap_dev, bs="4M", check=False)
        cmd("mkswap", swap_dev)
        thin_size_str = cmd_output(
            "thin_metadata_size",
            "--block-size=64k",
            f"--pool-size={DISK_SIZE}b",
            "--max-thins=1024",
            "-u=m",
            "-n",
        )
        thin_meta_pe = (math.ceil(float(thin_size_str)) + 3) // 4
        thin_meta_m = 4 * thin_meta_pe
        cmd(
            "lvcreate",
            "--type=thin-pool",
            f"--thinpool={thinpool_name}",
            "--chunksize=64k",
            f"--poolmetadatasize={thin_meta_m}m",
            "--size=4m",
            vg_name,
        )

        def move(src, dst, size):
            cmd(
                "pvmove",
                "--alloc=anywhere",
                f"{wrap_dev}:{SWAP_SIZE_PE + src}-{SWAP_SIZE_PE + src + size - 1}",
                f"{wrap_dev}:{SWAP_SIZE_PE + dst}-{SWAP_SIZE_PE + dst + size - 1}",
            )

        if MOVE_PES:
            move(thin_meta_pe + 1, 2 * thin_meta_pe + 1, thin_meta_pe)
            move(thin_meta_pe, 3 * thin_meta_pe + 1, 1)
            move(2 * thin_meta_pe + 1, thin_meta_pe, thin_meta_pe)
            move(3 * thin_meta_pe + 1, 2 * thin_meta_pe, 1)
        cmd("lvextend", "-l+100%FREE", thinpool_dev)
        pool_size = cmd_output(
            "lvs",
            "--units=m",
            "--noheadings",
            "-o",
            "lv_size",
            thinpool_dev,
        ).split()[0]
        cmd(
            "lvcreate",
            "--thin",
            f"--name={lv_name}",
            f"--virtualsize={pool_size}",
            thinpool_dev,
        )
        yield lv_dev
    finally:
        cmd("vgchange", "-an", vg_name)


@contextlib.contextmanager
def ext4_mount(wrap_dev):
    mount_name = f"ext4_mount_{str(uuid.uuid4()).replace('-', '_')}"
    mount_point = f"/mnt/{mount_name}"

    cmd("mkfs.ext4", wrap_dev, "-E", "nodiscard")
    os.mkdir(mount_point)
    try:
        cmd(
            "mount",
            wrap_dev,
            mount_point,
            "-o,defaults,nodiscard",
        )
        try:
            yield mount_point
        finally:
            cmd("umount", mount_point, check=False)
    finally:
        os.rmdir(mount_point)


@contextlib.contextmanager
def btrfs_mount(wrap_dev):
    mount_name = f"btrfs_mount_{str(uuid.uuid4()).replace('-', '_')}"
    mount_point = f"/mnt/{mount_name}"

    cmd(
        "mkfs.btrfs",
        wrap_dev,
        "--csum=xxhash",
        "--force",
        "--nodiscard",
    )
    os.mkdir(mount_point)
    try:
        cmd(
            "mount",
            wrap_dev,
            mount_point,
            "-o,defaults,relatime,lazytime,autodefrag,ssd,nodiscard,compress=zstd:3",
        )
        try:
            yield mount_point
        finally:
            cmd("umount", mount_point, check=False)
    finally:
        os.rmdir(mount_point)


@contextlib.contextmanager
def cascade(managers, prev, i=0, acc=None):
    if acc is None:
        acc = []
    if i == len(managers):
        yield acc
        return
    manager = managers[i]
    with manager(prev) as managed:
        acc.append(managed)
        with cascade(managers, managed, i + 1, acc) as res:
            yield res


def init_volumes(device: str, salt: str, hidden: bool):
    argon_cmd = [
        "argon2",
        salt,
        "-id",
        "-t=3",
        "-m=12",
        "-p=1",
        "-l=128",
        "-v=13",
        "-r",
    ]
    argon_cmd = [p for arg in argon_cmd for p in arg.split("=")]
    key_hex = (
        cmd(*argon_cmd, input=PASSWORD.encode(), stdout=subprocess.PIPE)
        .stdout.decode()
        .strip()
    )
    key_bytes = bytes.fromhex(key_hex)
    assert len(key_bytes) == 128
    with temp_file() as key_file:
        dd(of=key_file, input=key_bytes)
        meta_begin = DISK_SIZE - META_SIZE if hidden else ZERO_HEADER
        header_stack = [
            lambda dev: device_view(
                dmsetup_table(meta_begin, DISK_SIZE if hidden else META_SIZE, dev)
            ),
            functools.partial(plain_dmcrypt, key_file),
            integrity,
            ext4_mount,
        ]
        with cascade(header_stack, device) as (_, _, _, ext4_meta):
            cmd("cp", os.path.join(SCRIPT_DIR, "late.sh"), ext4_meta)
            with open(f"{ext4_meta}/table.txt", "w") as table:
                print(
                    (
                        reversed_table(0, DISK_SIZE - META_SIZE, "/dev/disk")
                        if hidden
                        else dmsetup_table(META_SIZE, DISK_SIZE, "/dev/disk")
                    ),
                    file=table,
                )
            header_file = f"{ext4_meta}/header.dat"
            data_stack = [
                lambda dev: device_view(
                    reversed_table(0, DISK_SIZE - META_SIZE, dev)
                    if hidden
                    else dmsetup_table(META_SIZE, DISK_SIZE, dev)
                ),
                functools.partial(detached_luks, key_file, "64", header_file),
                lvm,
                btrfs_mount,
            ]
            with cascade(data_stack, device) as (_, _, _, btrfs_data):
                cmd("mkdir", f"{btrfs_data}/boot")
                cmd(
                    "cp",
                    "/run/media/tom/vbox/linux/arch/x86_64/boot/bzImage",
                    f"{btrfs_data}/boot/vmlinuz-linux",
                )
                system_initrd(f"{btrfs_data}/boot/initramfs-linux.img")
                with open(f"{btrfs_data}/boot/cmdline-linux.txt", "w") as cmdline:
                    print("ro console=ttyS0", file=cmdline)
                with open(f"{btrfs_data}/message.txt", "w") as message:
                    if hidden:
                        print("HELLO FROM HIDDEN SYSTEM", file=message)
                    else:
                        print("HELLO FROM DECOY SYSTEM", file=message)


def setup_device(device: str, salt: str):
    with temp_file() as key_file:
        dd(_if="/dev/random", of=key_file, bs=64, count=1)
        with open(key_file, mode="rb") as key:
            shred_key = base64.encodebytes(key.read()).decode()
            print("Using shredding key", shred_key.replace("\n", ""))
        with plain_dmcrypt(key_file, device) as plain:
            dd(_if="/dev/zero", of=plain, bs="4M", status="progress", check=False)
    dd(_if="/dev/zero", of=device, bs="4K", count=1)
    init_volumes(device, salt, False)
    init_volumes(device, salt, True)


def run_qemu(disk_img: str, salt: str, hidden: bool):
    with temp_file(".cpio.gz") as initrd:
        bootstrap_initrd(initrd, salt, hidden)
        subprocess.run(
            [
                "qemu-system-x86_64",
                "-kernel",
                "/run/media/tom/vbox/linux/arch/x86_64/boot/bzImage",
                "-initrd",
                initrd,
                "-nographic",
                "-append",
                "ro console=ttyS0",
                "-enable-kvm",
                "-m",
                "1G",
                "-hda",
                disk_img,
            ]
        )


def main() -> int:
    salt4k = dd(_if="/dev/random", bs="4K", count=1, stdout=subprocess.PIPE).stdout
    salt = cmd_output("sha512sum", "--binary", input=salt4k).split()[0]

    with temp_file(".img") as disk_img:
        dd(_if="/dev/zero", of=disk_img, bs=DISK_SIZE, count=1, status="progress")
        with loopback_device(disk_img) as loop:
            setup_device(loop, salt)
            run_qemu(disk_img, salt, False)
            run_qemu(disk_img, salt, True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
