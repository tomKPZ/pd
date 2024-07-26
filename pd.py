#!/usr/bin/env python3

import contextlib
import functools
import getpass
import hashlib
import math
import os
import subprocess
import sys
import tempfile
import uuid

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB

# Config
DD_DEFAULT_BS = 4 * MIB
PARTITION_ALIGN = 1 * MIB
ZERO_HEADER = 1 * MIB
CRYPT_SECTOR_SIZE = 4 * KIB
CRYPT_KEY_SIZE = 512
SWAP_SIZE = 64 * MIB
PE_SIZE = 4 * MIB
THIN_CHUNK_SIZE = 64 * KIB
META_SIZE = 128 * MIB
MOVE_PES = False
REVERSE_CHUNK_SIZE = 8 * MIB
INITRD_XZ_LEVEL = "1"
USE_STATIC_TOOLS = True
HIDDEN_SALT_SUFFIX = "a"
ARGON2_SECURE = False
if ARGON2_SECURE:
    ARGON2_ITERATIONS = 14
    ARGON2_M_KB_LOG2 = 18
    ARGON2_THREADS = 16
else:
    ARGON2_ITERATIONS = 2
    ARGON2_M_KB_LOG2 = 15
    ARGON2_THREADS = 4
USE_TEST_PASSWORD = True
if USE_TEST_PASSWORD:
    TEST_PASSWORD = "testpassword"
SECURE_RAND = True
KERNEL_PATH = "/run/media/tom/vbox/linux/arch/x86_64/boot/bzImage"
INSTALL = False
if INSTALL:
    INSTALL_DEVICE = "/dev/nvme0n1p10"
    INSTALL_UKI = True
    UKI_INSTALL_PATH = "/mnt/EFI/BOOT/BOOTX64.EFI"
else:
    TEST_DISK_SIZE = 1 * GIB
    RUN_QEMU = True
DEBUG_TRACE_CMDS = True

# Config checks and non-modifiable constants
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
assert ZERO_HEADER % PARTITION_ALIGN == 0
assert META_SIZE % PARTITION_ALIGN == 0
assert REVERSE_CHUNK_SIZE % PARTITION_ALIGN == 0
assert SWAP_SIZE % PE_SIZE == 0
if USE_STATIC_TOOLS:
    SRC_ROOT = os.path.join(SCRIPT_DIR, "static-tools") + "/"
else:
    SRC_ROOT = "/"
assert SRC_ROOT.startswith("/")
assert SRC_ROOT.endswith("/")
SWAP_SIZE_PE = SWAP_SIZE // PE_SIZE
DM_SECTOR_SIZE = 512

ARGON2_ARGS = [
    "-id",
    f"-t={ARGON2_ITERATIONS}",
    f"-m={ARGON2_M_KB_LOG2}",
    f"-p={ARGON2_THREADS}",
    "-l=512",
    "-v=13",
    "-r",
]
ARGON2_ARGS = [p for arg in ARGON2_ARGS for p in arg.split("=")]

CRYPT_ARGS = [
    "--cipher=aes-xts-plain64",
    f"--key-size={CRYPT_KEY_SIZE}",
    f"--sector-size={CRYPT_SECTOR_SIZE}",
]

CMD_TRACE = []


def cmd(*args, check=True, **kwargs):
    CMD_TRACE.append(args)
    return subprocess.run(args, check=check, **kwargs)


def cmd_output(*args, **kwargs):
    return cmd(*args, stdout=subprocess.PIPE, **kwargs).stdout.decode().strip()


def rand512() -> bytes:
    if SECURE_RAND:
        rand4k = cmd("gpg", "--gen-random", "2", "512", stdout=subprocess.PIPE).stdout
    else:
        rand4k = os.urandom(512)
    return hashlib.sha512(rand4k).digest()


def device_size(device: str) -> int:
    size = int(cmd_output("blockdev", "--getsize64", device))
    return size // PARTITION_ALIGN * PARTITION_ALIGN


def add_file(root, file):
    real_file = os.path.join(SRC_ROOT, file)
    dst = os.path.join(root, file.lstrip("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    cmd("cp", real_file, dst)


def add_library(root, lib):
    dst = os.path.join(root, lib.removeprefix(SRC_ROOT))
    if not os.path.isfile(dst):
        add_file(root, lib)
        cmd("strip", "--strip-unneeded", dst)


def add_binary(root, bin):
    real_bin = os.path.join(SRC_ROOT, bin)
    add_file(root, bin)
    dst = os.path.join(root, bin.removeprefix(SRC_ROOT))
    cmd("strip", "--strip-all", dst)

    links = cmd_output(
        "find",
        "-L",
        os.path.dirname(real_bin),
        "-samefile",
        real_bin,
        "-xtype",
        "l",
    ).splitlines()

    for link in links:
        link_path = os.path.join(root, link.removeprefix(SRC_ROOT))
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        os.symlink(os.path.basename(bin), link_path)

    dependencies = cmd_output("ldd", real_bin, check=False).splitlines()
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

    bins = [
        "usr/bin/argon2",
        "usr/bin/cryptsetup",
        "usr/bin/dmsetup",
    ]

    for bin in bins:
        add_binary(root, bin)

    add_binary(root, "usr/bin/busybox")
    if not USE_STATIC_TOOLS:
        applets = cmd_output(
            os.path.join(SRC_ROOT, "usr/bin/busybox"), "--list"
        ).splitlines()
        for applet in applets:
            if applet != "busybox":
                os.symlink("busybox", os.path.join(root, "usr", "bin", applet))

    os.symlink("../proc/self/mounts", os.path.join(root, "etc", "mtab"))
    open(os.path.join(root, "etc", "fstab"), "w").close()
    open(os.path.join(root, "etc", "ld.so.conf"), "w").close()

    cmd("cp", os.path.join(SCRIPT_DIR, "init"), root)


def bootstrap_initrd(out_file: str, salt: str, mods: list[str] = []):
    with tempfile.TemporaryDirectory() as root:
        common_initrd(root)

        with open(os.path.join(root, "salt.txt"), "w") as salt_file:
            salt_file.write(salt)

        with open(os.path.join(SCRIPT_DIR, "early.sh")) as src:
            with open(os.path.join(root, "early.sh"), "w") as dst:
                read_password = (
                    f"password={TEST_PASSWORD}"
                    if USE_TEST_PASSWORD
                    else "read -sp 'Password: ' password\necho"
                )
                format_args = {
                    "argon_args": " ".join(ARGON2_ARGS),
                    "crypt_args": " ".join(CRYPT_ARGS),
                    "read_password": read_password,
                }
                dst.write(src.read().format(**format_args))
            os.chmod(os.path.join(root, "early.sh"), 0o755)

        table_file = os.path.join(root, "table.txt")
        with open(table_file, "w") as table:
            print(dmsetup_table(ZERO_HEADER, META_SIZE, "/boot_disk"), file=table)

        for mod in mods:
            cmd(mod, shell=True, cwd=root)

        cmd(
            f"find . -print0 | cpio --null -o --format=newc | xz --check=crc32 -{INITRD_XZ_LEVEL} >{out_file}",
            cwd=root,
            shell=True,
        )


def system_initrd(out_file):
    with tempfile.TemporaryDirectory() as root:
        common_initrd(root)
        add_binary(root, "usr/bin/lvm")
        add_binary(root, "usr/bin/pdata_tools")

        cmd("cp", os.path.join(SCRIPT_DIR, "system.sh"), os.path.join(root, "early.sh"))

        cmd(
            f"find . -print0 | cpio --null -o --format=newc | xz --check=crc32 -{INITRD_XZ_LEVEL} >{out_file}",
            cwd=root,
            shell=True,
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
    start_sector, start_rem = divmod(start, DM_SECTOR_SIZE)
    end_sector, end_rem = divmod(end, DM_SECTOR_SIZE)
    assert start_rem == 0 and end_rem == 0
    return f"0 {end_sector-start_sector} linear {wrap_dev} {start_sector}"


def reversed_table(start: int, end: int, wrap_dev: str):
    sectors_per_chunk = REVERSE_CHUNK_SIZE // DM_SECTOR_SIZE

    start_sector, start_rem = divmod(start, DM_SECTOR_SIZE)
    end_sector, end_rem = divmod(end, DM_SECTOR_SIZE)
    assert start_rem == 0 and end_rem == 0

    num_sectors = end_sector - start_sector
    num_chunks = num_sectors // sectors_per_chunk

    dmsetup_input = []
    for i in range(num_chunks):
        start = i * sectors_per_chunk
        reversed_start = num_sectors - (i + 1) * sectors_per_chunk
        dmsetup_input.append(
            f"{start_sector + start} {sectors_per_chunk} linear {wrap_dev} {reversed_start}"
        )
    return "\n".join(dmsetup_input) + "\n"


def dev_uuid() -> str:
    return str(uuid.uuid4()).replace("-", "_")


@contextlib.contextmanager
def device_view(table: str):
    device_name = f"view_{dev_uuid()}"
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
    crypt_name = f"dmcrypt_test_{dev_uuid()}"
    crypt_dev = f"/dev/mapper/{crypt_name}"
    cmd(
        "cryptsetup",
        "open",
        "--type=plain",
        *CRYPT_ARGS,
        f"--key-file={key_file}",
        wrap_dev,
        crypt_name,
    )
    try:
        yield crypt_dev
    finally:
        cmd("cryptsetup", "close", crypt_name)


@contextlib.contextmanager
def detached_luks(key_file, key_file_offset, header_file, wrap_dev):
    luks_name = f"luks_test_{dev_uuid()}"
    luks_dev = f"/dev/mapper/{luks_name}"

    with temp_file() as volume_key:
        with open(volume_key, "wb") as f:
            f.write(rand512())
        cmd(
            "cryptsetup",
            "luksFormat",
            "--batch-mode",
            "--use-random",
            "--hash=sha512",
            *CRYPT_ARGS,
            f"--key-file={key_file}",
            f"--keyfile-offset={key_file_offset}",
            f"--header={header_file}",
            f"--volume-key-file={volume_key}",
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
def lvm(vg_name, wrap_dev):
    lv_name = "test_lv"
    swap_name = "swap"
    swap_dev = f"/dev/{vg_name}/{swap_name}"
    thinpool_name = "thinpool"
    thinpool_dev = f"/dev/{vg_name}/{thinpool_name}"
    lv_dev = f"/dev/{vg_name}/{lv_name}"

    dd(_if="/dev/zero", of=wrap_dev, bs=PE_SIZE, count=1)
    cmd("pvcreate", wrap_dev)
    cmd(
        "vgcreate",
        f"--physicalextentsize={PE_SIZE}b",
        vg_name,
        wrap_dev,
    )
    try:
        cmd(
            "lvcreate",
            "--type=linear",
            f"--size={SWAP_SIZE}b",
            f"--name={swap_name}",
            vg_name,
        )
        dd(_if="/dev/zero", of=swap_dev, bs=DD_DEFAULT_BS, check=False)
        cmd("mkswap", swap_dev)
        thin_size_str = cmd_output(
            "thin_metadata_size",
            f"--block-size={THIN_CHUNK_SIZE}b",
            f"--pool-size={device_size(wrap_dev)}b",
            "--max-thins=1000",
            "-u=b",
            "-n",
        )
        thin_meta_pe = (math.ceil(float(thin_size_str)) + (PE_SIZE - 1)) // PE_SIZE
        cmd(
            "lvcreate",
            "--type=thin-pool",
            f"--thinpool={thinpool_name}",
            f"--chunksize={THIN_CHUNK_SIZE}b",
            f"--poolmetadatasize={thin_meta_pe * PE_SIZE}b",
            f"--size={PE_SIZE}b",
            "--discards=nopassdown",
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
def btrfs_header_mount(wrap_dev):
    mount_name = f"btrfs_mount_{dev_uuid()}"
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
            "-o,defaults,relatime,lazytime,autodefrag,ssd,nodiscard,compress=zstd:12",
        )
        try:
            yield mount_point
        finally:
            cmd("umount", mount_point, check=False)
    finally:
        os.rmdir(mount_point)


@contextlib.contextmanager
def btrfs_system_mount(wrap_dev):
    mount_name = f"btrfs_mount_{dev_uuid()}"
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
            "-o,defaults,relatime,lazytime,autodefrag,ssd,nodiscard,compress=zstd:12",
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


def read_password(pw_type):
    if USE_TEST_PASSWORD:
        return TEST_PASSWORD
    while True:
        password1 = getpass.getpass(f"{pw_type} password: ")
        password2 = getpass.getpass(f"Confirm {pw_type} password: ")
        if password1 == password2:
            return password1
        else:
            print("Passwords do not match. Please try again.")


def init_volumes(device: str, salt: str, hidden: bool):
    salt_suffix = HIDDEN_SALT_SUFFIX if hidden else ""
    argon_cmd = ["argon2", salt + salt_suffix] + ARGON2_ARGS
    password = read_password("Hidden" if hidden else "Decoy")
    key_hex = cmd_output(*argon_cmd, input=password.encode())
    key_bytes = bytes.fromhex(key_hex)
    dev_size = device_size(device)
    with temp_file() as key_file:
        dd(of=key_file, input=key_bytes)
        meta_begin = dev_size - META_SIZE if hidden else ZERO_HEADER
        meta_end = dev_size - ZERO_HEADER if hidden else META_SIZE
        header_stack = [
            lambda dev: device_view(dmsetup_table(meta_begin, meta_end, dev)),
            functools.partial(plain_dmcrypt, key_file),
            btrfs_header_mount,
        ]
        with cascade(header_stack, device) as (_, _, btrfs_meta):
            root = os.path.join(btrfs_meta, "root")
            os.makedirs(os.path.join(root, "etc", "lvm"), exist_ok=True)
            open(os.path.join(root, "etc", "lvm", "lvm.conf"), "w").close()
            add_binary(root, "usr/bin/kexec")
            add_binary(root, "usr/bin/lvm")
            add_binary(root, "usr/bin/pdata_tools")
            cmd("cp", os.path.join(SCRIPT_DIR, "late.sh"), btrfs_meta)
            with open(f"{btrfs_meta}/table.txt", "w") as table:
                print(
                    (
                        reversed_table(0, dev_size - META_SIZE, "/boot_disk")
                        if hidden
                        else dmsetup_table(META_SIZE, dev_size, "/boot_disk")
                    ),
                    file=table,
                )
            header_file = f"{btrfs_meta}/header.dat"
            vg_name = "sysvg1" if hidden else "sysvg"
            data_stack = [
                lambda dev: device_view(
                    reversed_table(0, dev_size - META_SIZE, dev)
                    if hidden
                    else dmsetup_table(META_SIZE, dev_size, dev)
                ),
                functools.partial(detached_luks, key_file, "64", header_file),
                functools.partial(lvm, vg_name),
                btrfs_system_mount,
            ]
            with cascade(data_stack, device) as (_, _, _, btrfs_data):
                cmd("mkdir", f"{btrfs_data}/boot")
                cmd("cp", KERNEL_PATH, f"{btrfs_data}/boot/vmlinuz-linux")
                system_initrd(f"{btrfs_data}/boot/initramfs-linux.img")
                with open(f"{btrfs_data}/boot/cmdline-linux.txt", "w") as cmdline:
                    print("quiet" if INSTALL else "console=ttyS0", file=cmdline)
                with open(f"{btrfs_data}/message.txt", "w") as message:
                    if hidden:
                        print("HELLO FROM HIDDEN SYSTEM", file=message)
                    else:
                        print("HELLO FROM DECOY SYSTEM", file=message)


def setup_device(device: str, shred_key: bytes, salt: str):
    with temp_file() as key_file:
        with open(key_file, mode="wb") as key:
            key.write(shred_key)
        with plain_dmcrypt(key_file, device) as plain:
            dd(
                _if="/dev/zero",
                of=plain,
                bs=DD_DEFAULT_BS,
                status="progress",
                check=False,
            )
    dd(_if="/dev/zero", of=device, bs="4K", count=1)
    init_volumes(device, salt, False)
    init_volumes(device, salt, True)


def run_qemu(disk_img: str, initrd: str):
    cmd(
        "qemu-system-x86_64",
        "-kernel",
        KERNEL_PATH,
        "-initrd",
        initrd,
        "-nographic",
        "-append",
        "console=ttyS0",
        "-enable-kvm",
        "-m",
        "1G",
        "-drive",
        f"file={disk_img},if=none,id=nvm",
        "-device",
        "nvme,serial=deadbeef,drive=nvm",
    )


def hidden_mods(disk_size: int) -> list[str]:
    start_sector = ZERO_HEADER // DM_SECTOR_SIZE
    end_sector = (disk_size - META_SIZE) // DM_SECTOR_SIZE
    return [
        f"sed -i s/{start_sector}/{end_sector}/ table.txt",
        "echo a >> salt.txt",
    ]


def build_uki(salt: str):
    with temp_file(".cpio.xz") as initrd:
        bootstrap_initrd(initrd, salt)
        cmd(
            "ukify",
            "build",
            "--linux",
            KERNEL_PATH,
            "--initrd",
            initrd,
            "--cmdline",
            "quiet ro",
            "--output",
            UKI_INSTALL_PATH,
        )


def main() -> int:
    shred_key = rand512()
    salt = rand512().hex()
    mods = []
    if INSTALL:
        setup_device(INSTALL_DEVICE, shred_key, salt)
        if INSTALL_UKI:
            build_uki(salt)
        mods = hidden_mods(device_size(INSTALL_DEVICE))
    else:
        with temp_file(".img") as disk_img:
            dd(
                _if="/dev/zero",
                of=disk_img,
                bs=TEST_DISK_SIZE,
                count=1,
                status="progress",
            )
            with loopback_device(disk_img) as loop:
                setup_device(loop, shred_key, salt)
                if RUN_QEMU:
                    with temp_file(".cpio.xz") as decoy_initrd:
                        bootstrap_initrd(decoy_initrd, salt)
                        run_qemu(disk_img, decoy_initrd)
                    with temp_file(".cpio.xz") as hidden_initrd:
                        mods = hidden_mods(TEST_DISK_SIZE)
                        bootstrap_initrd(hidden_initrd, salt, mods)
                        run_qemu(disk_img, hidden_initrd)
    if DEBUG_TRACE_CMDS:
        print("Command trace:")
        for traced in CMD_TRACE:
            print(" ".join(traced))
    print("Shredding key", shred_key.hex())
    print("Hidden mods:")
    for mod in mods:
        print(mod)
    return 0


if __name__ == "__main__":
    sys.exit(main())
