#!/usr/bin/env python3

import contextlib
import functools
import getpass
import hashlib
import math
import pathlib
import subprocess
import sys
import tempfile
import uuid

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB

# Config
SHRED_DEVICE = False
INIT_DECOY = True
INIT_HIDDEN = True
VG_NAME = "sysvg"
CREATE_SWAP = True
SWAP_SIZE = 512 * MIB
DD_DEFAULT_BS = 4 * MIB
PARTITION_ALIGN = 1 * MIB
ZERO_HEADER = 1 * MIB
CRYPT_SECTOR_SIZE = 4 * KIB
CRYPT_KEY_SIZE = 512
PE_SIZE = 4 * MIB
THIN_CHUNK_SIZE = 64 * KIB
META_SIZE = 256 * MIB
MOVE_PES = False
REVERSE_CHUNK_SIZE = 8 * MIB
INITRD_XZ_LEVEL = "1"
USE_STATIC_TOOLS = False
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
USE_TEST_PASSWORD = False
TEST_PASSWORD = "testpassword"
INSTALL_ARCH = True
KERNEL_PATH = pathlib.Path("/run/media/tom/vbox/linux/arch/x86_64/boot/bzImage")
INSTALL = True
INSTALL_DEVICE = pathlib.Path("/dev/nvme0n1p10")
INSTALL_UKI = True
INSTALL_UKI_PATH = pathlib.Path("/mnt/EFI/BOOT/BOOTX64.EFI")
TEST_DISK_SIZE = 8 * GIB
RUN_QEMU = True
DEBUG_TRACE_CMDS = False

# Config checks and non-modifiable constants
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
assert TEST_DISK_SIZE % DD_DEFAULT_BS == 0
assert ZERO_HEADER % PARTITION_ALIGN == 0
assert META_SIZE % PARTITION_ALIGN == 0
assert REVERSE_CHUNK_SIZE % PARTITION_ALIGN == 0
assert SWAP_SIZE % PE_SIZE == 0
SRC_ROOT = SCRIPT_DIR / "static-tools" if USE_STATIC_TOOLS else pathlib.Path("/")
assert SRC_ROOT.is_absolute()
SWAP_SIZE_PE = SWAP_SIZE // PE_SIZE
DM_SECTOR_SIZE = 512
DEV = pathlib.Path("/dev")
MNT = pathlib.Path("/mnt")
USR_BIN = pathlib.Path("usr/bin")

BTRFS_SUBVOLS = {
    "@": "",
    "@home": "home",
    "@var_cache": "var/cache",
    "@var_lib_docker": "var/lib/docker",
    "@var_log": "var/log",
    "@var_tmp": "var/tmp",
}

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


def rand512() -> bytes:
    rand8k = cmd("gpg", "--gen-random", "2", "512", stdout=subprocess.PIPE).stdout
    rand8k += dd(_if="/dev/random", bs=512, count=1, stdout=subprocess.PIPE).stdout
    return hashlib.sha512(rand8k).digest()


def device_size(device: pathlib.Path) -> int:
    size = int(cmd_output("blockdev", "--getsize64", device))
    return size // PARTITION_ALIGN * PARTITION_ALIGN


@functools.cache
def mkdirs(path: pathlib.Path):
    cmd("mkdir", "-p", path)


def add_file(root: pathlib.Path, file: pathlib.Path):
    assert root.is_absolute()
    assert not file.is_absolute()
    real_file = SRC_ROOT / file
    dst = root / file
    mkdirs(dst.parent)
    cmd("cp", real_file, dst)


def add_library(root: pathlib.Path, lib: pathlib.Path):
    assert root.is_absolute()
    assert not lib.is_absolute()
    dst = root / lib
    if not dst.is_file():
        add_file(root, lib)
        cmd("strip", "--strip-unneeded", dst)


def add_binary(root: pathlib.Path, bin: pathlib.Path):
    assert root.is_absolute()
    assert not bin.is_absolute()
    real_bin = SRC_ROOT / bin
    add_file(root, bin)
    dst = root / bin
    cmd("strip", "--strip-all", dst)

    links = cmd_output(
        "find",
        "-L",
        real_bin.parent,
        "-samefile",
        real_bin,
        "-xtype",
        "l",
    ).splitlines()

    for link in links:
        link_path = root / pathlib.Path(link).relative_to(SRC_ROOT)
        mkdirs(link_path.parent)
        cmd("ln", "-s", bin.name, link_path)

    dependencies = cmd_output("ldd", real_bin, check=False).splitlines()
    for dep in dependencies:
        dep_path = (
            dep.split()[2]
            if len(dep.split()) > 2 and dep.split()[2].startswith("/")
            else None
        )
        if dep_path:
            add_library(root, pathlib.Path(dep_path).relative_to(SRC_ROOT))


def common_initrd(root: pathlib.Path):
    mkdirs(root / "dev")
    mkdirs(root / "etc")
    mkdirs(root / "mnt")
    mkdirs(root / "proc")
    mkdirs(root / "run")
    mkdirs(root / "sys")
    mkdirs(root / "tmp")
    mkdirs(root / "usr" / "bin")
    mkdirs(root / "usr" / "lib")
    mkdirs(root / "var")
    cmd("ln", "-s", "usr/lib", root / "lib")
    cmd("ln", "-s", "bin", root / "usr" / "sbin")
    cmd("ln", "-s", "usr/bin", root / "bin")
    cmd("ln", "-s", "usr/bin", root / "sbin")
    cmd("ln", "-s", "../run", root / "var" / "run")
    cmd("ln", "-s", "lib", root / "usr" / "lib64")
    cmd("ln", "-s", "usr/lib", root / "lib64")

    bins = [
        USR_BIN / "argon2",
        USR_BIN / "cryptsetup",
        USR_BIN / "dmsetup",
    ]
    for bin in bins:
        add_binary(root, bin)

    add_binary(root, USR_BIN / "busybox")
    if not USE_STATIC_TOOLS:
        applets = cmd_output(SRC_ROOT / USR_BIN / "busybox", "--list").splitlines()
        for applet in applets:
            if applet != "busybox":
                cmd("ln", "-s", "busybox", root / "usr" / "bin" / applet)

    cmd("ln", "-s", "../proc/self/mounts", root / "etc" / "mtab")
    cmd("touch", root / "etc" / "fstab")
    cmd("touch", root / "etc" / "ld.so.conf")

    cmd("cp", SCRIPT_DIR / "init", root)


def bootstrap_initrd(out_file: pathlib.Path, salt: str, mods: list[str] = []):
    with temp_dir() as root:
        common_initrd(root)

        with open(root / "salt.txt", "w") as salt_file:
            salt_file.write(salt)

        cmd("cp", SCRIPT_DIR / "early.sh", root)
        with open(root / "early_env.sh", "w") as early_env:
            env = {
                "ARGON_ARGS": " ".join(ARGON2_ARGS),
                "CRYPT_ARGS": " ".join(CRYPT_ARGS),
            }
            for k, v in env.items():
                print(f'{k}="{v}"', file=early_env)
            print("read_password() {", file=early_env)
            if USE_TEST_PASSWORD:
                print(f"  PASSWORD={TEST_PASSWORD}", file=early_env)
            else:
                print(f"  read -sp 'Password: ' PASSWORD\necho", file=early_env)
            print("}", file=early_env)

        table_file = root / "table.txt"
        with open(table_file, "w") as table:
            print(
                dmsetup_table(ZERO_HEADER, META_SIZE, pathlib.Path("/boot_disk")),
                file=table,
            )

        for mod in mods:
            cmd(mod, shell=True, cwd=root)

        cmd(
            f"find . -print0 | cpio --null -o --format=newc | xz --check=crc32 -{INITRD_XZ_LEVEL} >{out_file}",
            cwd=root,
            shell=True,
        )


def system_initrd(out_file: pathlib.Path):
    with temp_dir() as root:
        common_initrd(root)
        add_binary(root, USR_BIN / "lvm")
        add_binary(root, USR_BIN / "pdata_tools")

        cmd("cp", SCRIPT_DIR / "system.sh", root / "early.sh")
        write_late_env(root / "system_env.sh")

        cmd(
            f"find . -print0 | cpio --null -o --format=newc | xz --check=crc32 -{INITRD_XZ_LEVEL} >{out_file}",
            cwd=root,
            shell=True,
        )


@contextlib.contextmanager
def loopback_device(file_path: pathlib.Path):
    loop_dev = cmd_output("losetup", "-f", "--show", file_path)
    try:
        yield loop_dev
    finally:
        cmd("losetup", "-d", loop_dev)


@contextlib.contextmanager
def temp_file(suffix=""):
    file = tempfile.mktemp(suffix=suffix)
    try:
        yield pathlib.Path(file)
    finally:
        cmd("rm", file)


@contextlib.contextmanager
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield pathlib.Path(tmp)


def dmsetup_table(start: int, end: int, wrap_dev: pathlib.Path):
    start_sector, start_rem = divmod(start, DM_SECTOR_SIZE)
    end_sector, end_rem = divmod(end, DM_SECTOR_SIZE)
    assert start_rem == 0 and end_rem == 0
    return f"0 {end_sector - start_sector} linear {wrap_dev} {start_sector}"


def reversed_table(start: int, end: int, wrap_dev: pathlib.Path):
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
    device_path = DEV / "mapper" / device_name

    cmd(
        "dmsetup",
        "create",
        device_path.name,
        input=table.encode(),
    )
    try:
        yield device_path
    finally:
        cmd("dmsetup", "remove", device_path)


@contextlib.contextmanager
def plain_dmcrypt(key_file: pathlib.Path, wrap_dev: pathlib.Path):
    crypt_name = f"dmcrypt_test_{dev_uuid()}"
    crypt_dev = DEV / "mapper" / crypt_name
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
def detached_luks(
    key_file: pathlib.Path,
    key_file_offset: int,
    header_file: pathlib.Path,
    wrap_dev: pathlib.Path,
):
    luks_name = f"luks_test_{dev_uuid()}"
    luks_dev = DEV / "mapper" / luks_name

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
def lvm(wrap_dev: pathlib.Path):
    lv_name = "root"
    swap_name = "swap"
    swap_dev = DEV / VG_NAME / swap_name
    thinpool_name = "thinpool"
    thinpool_dev = DEV / VG_NAME / thinpool_name
    lv_dev = DEV / VG_NAME / lv_name

    dd(_if="/dev/zero", of=wrap_dev, bs=PE_SIZE, count=1)
    cmd("pvcreate", wrap_dev)
    cmd(
        "vgcreate",
        f"--physicalextentsize={PE_SIZE}b",
        VG_NAME,
        wrap_dev,
    )
    try:
        if CREATE_SWAP:
            cmd(
                "lvcreate",
                "--type=linear",
                f"--size={SWAP_SIZE}b",
                f"--name={swap_name}",
                VG_NAME,
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
            VG_NAME,
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
        cmd("vgchange", "-an", VG_NAME)


@contextlib.contextmanager
def btrfs_header_mount(wrap_dev: pathlib.Path):
    mount_name = f"btrfs_mount_{dev_uuid()}"
    mount_point = MNT / mount_name

    cmd(
        "mkfs.btrfs",
        wrap_dev,
        "--data=dup",
        "--csum=blake2",
        "--force",
        "--nodiscard",
    )
    cmd("mkdir", mount_point)
    try:
        cmd(
            "mount",
            wrap_dev,
            mount_point,
            "-o,defaults,noatime,autodefrag,ssd,nodiscard,compress=zstd:15",
        )
        try:
            yield mount_point
        finally:
            cmd("umount", mount_point, check=False)
    finally:
        cmd("rmdir", mount_point)


@contextlib.contextmanager
def btrfs_system_mount(wrap_dev: pathlib.Path):
    mount_name = f"btrfs_mount_{dev_uuid()}"
    mount_point = MNT / mount_name

    cmd(
        "mkfs.btrfs",
        wrap_dev,
        "--csum=xxhash",
        "--force",
        "--nodiscard",
    )
    cmd("mkdir", mount_point)
    OPTS = [
        "-o",
        "defaults",
        "relatime",
        "lazytime",
        "autodefrag",
        "ssd",
        "nodiscard",
        "compress=zstd:12",
    ]
    try:
        cmd("mount", wrap_dev, mount_point, ",".join(OPTS + ["subvolid=5"]))
        try:
            for subvol in BTRFS_SUBVOLS.keys():
                cmd("btrfs", "subvolume", "create", mount_point / subvol)
        finally:
            cmd("umount", mount_point, check=False)
        try:
            for name, path in BTRFS_SUBVOLS.items():
                mkdirs(mount_point / path)
                cmd(
                    "mount",
                    wrap_dev,
                    mount_point / path,
                    ",".join(OPTS + [f"subvol={name}"]),
                )
            yield mount_point
        finally:
            for path in reversed(BTRFS_SUBVOLS.values()):
                cmd("umount", mount_point / path, check=False)
    finally:
        cmd("rmdir", mount_point)


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


def read_password(pw_type: str):
    if USE_TEST_PASSWORD:
        return TEST_PASSWORD
    while True:
        password1 = getpass.getpass(f"{pw_type} password: ")
        password2 = getpass.getpass(f"Confirm {pw_type} password: ")
        if password1 == password2:
            return password1
        else:
            print("Passwords do not match. Please try again.")


def write_late_env(dst: pathlib.Path):
    with open(dst, "w") as late_env:
        print(f"VG={VG_NAME}", file=late_env)
        print("link_tmeta() {", file=late_env)
        if USE_STATIC_TOOLS:
            print("  true", file=late_env)
        else:
            print(
                f"  ln -s /dev/mapper/luks /dev/mapper/{VG_NAME}-thinpool_tmeta",
                file=late_env,
            )
        print("}", file=late_env)


def install_arch(root: pathlib.Path):
    PACKAGES = [
        "base",
        "linux",
        "linux-firmware",
        "lvm2",
        "btrfs-progs",
    ]
    cmd("pacstrap", "-c", root, *PACKAGES)
    cmd("cp", SCRIPT_DIR / "encrypt_install.sh", root / "etc/initcpio/install/mycrypt")
    cmd("cp", SCRIPT_DIR / "encrypt_hook.sh", root / "etc/initcpio/hooks/mycrypt")
    HOOKS = [
        "base",
        "udev",
        "autodetect",
        "modconf",
        "kms",
        "keyboard",
        "keymap",
        "consolefont",
        "block",
        "mycrypt",
        "lvm2",
        "filesystems",
        "resume",
        "fsck",
    ]
    cmd(
        "sed",
        "-i",
        f"s/^HOOKS=.*/HOOKS=({' '.join(HOOKS)})/",
        root / "etc/mkinitcpio.conf",
    )
    OPTS = [
        "defaults",
        "relatime",
        "lazytime",
        "autodefrag",
        "ssd",
        "nodiscard",
        "compress=zstd:12",
    ]
    with open(root / "etc/fstab", "w") as fstab:
        for name, path in BTRFS_SUBVOLS.items():
            print(
                f"/dev/{VG_NAME}/root",
                "/" + path,
                "btrfs",
                ",".join(OPTS + [f"subvol={name}"]),
                0,
                0,
                file=fstab,
            )
        print(
            f"/dev/{VG_NAME}/swap",
            "swap",
            "swap",
            "defaults,relatime,lazytime,ssd,nodiscard",
            0,
            0,
            file=fstab,
        )
    cmd("arch-chroot", root, "mkinitcpio", "-P")
    cmd("arch-chroot", root, "passwd", "root")
    with open(root / "boot/cmdline-linux.txt", "w") as cmdline:
        KERNEL_CMDLINE = [
            "rw",
            f"root=/dev/{VG_NAME}/root",
            "rootflags=subvol=@",
            f"resume=/dev/{VG_NAME}/swap",
        ]
        if not INSTALL:
            KERNEL_CMDLINE.append("console=ttyS0")
        print(*KERNEL_CMDLINE, file=cmdline)


def init_volumes(device: pathlib.Path, salt: str, hidden: bool):
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
            root = btrfs_meta / "root"
            cmd("mkdir", "-p", root / "etc" / "lvm")
            cmd("touch", root / "etc" / "lvm" / "lvm.conf")
            add_binary(root, USR_BIN / "kexec")
            add_binary(root, USR_BIN / "lvm")
            add_binary(root, USR_BIN / "pdata_tools")
            cmd("cp", SCRIPT_DIR / "late.sh", btrfs_meta)
            write_late_env(btrfs_meta / "late_env.sh")
            with open(f"{btrfs_meta}/table.txt", "w") as table:
                boot_disk = pathlib.Path("/boot_disk")
                print(
                    (
                        reversed_table(0, dev_size - META_SIZE, boot_disk)
                        if hidden
                        else dmsetup_table(META_SIZE, dev_size, boot_disk)
                    ),
                    file=table,
                )
            header_file = btrfs_meta / "header.dat"
            data_stack = [
                lambda dev: device_view(
                    reversed_table(0, dev_size - META_SIZE, dev)
                    if hidden
                    else dmsetup_table(META_SIZE, dev_size, dev)
                ),
                functools.partial(detached_luks, key_file, 64, header_file),
                lvm,
                btrfs_system_mount,
            ]
            with cascade(data_stack, device) as (_, _, _, btrfs_data):
                if INSTALL_ARCH:
                    install_arch(btrfs_data)
                else:
                    cmd("mkdir", f"{btrfs_data}/boot")
                    cmd("cp", KERNEL_PATH, f"{btrfs_data}/boot/vmlinuz-linux")
                    system_initrd(btrfs_data / "boot/initramfs-linux.img")
                    with open(btrfs_data / "message.txt", "w") as message:
                        if hidden:
                            print("HELLO FROM HIDDEN SYSTEM", file=message)
                        else:
                            print("HELLO FROM DECOY SYSTEM", file=message)
                    with open(btrfs_data / "boot/cmdline-linux.txt", "w") as cmdline:
                        print("quiet" if INSTALL else "console=ttyS0", file=cmdline)


def shred_device(device: pathlib.Path, shred_key: bytes):
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


def setup_device(device: pathlib.Path, shred_key: bytes, salt: str):
    if SHRED_DEVICE:
        shred_device(device, shred_key)
    dd(_if="/dev/zero", of=device, bs=ZERO_HEADER, count=1)
    if INIT_DECOY:
        init_volumes(device, salt, False)
    if INIT_HIDDEN:
        init_volumes(device, salt, True)


def run_qemu(disk_img: pathlib.Path, initrd: pathlib.Path):
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
            INSTALL_UKI_PATH,
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
                bs=DD_DEFAULT_BS,
                count=TEST_DISK_SIZE // DD_DEFAULT_BS,
                status="progress",
            )
            with loopback_device(disk_img) as loop:
                setup_device(loop, shred_key, salt)
                if RUN_QEMU:
                    if INIT_DECOY:
                        with temp_file(".cpio.xz") as decoy_initrd:
                            bootstrap_initrd(decoy_initrd, salt)
                            run_qemu(disk_img, decoy_initrd)
                    if INIT_HIDDEN:
                        with temp_file(".cpio.xz") as hidden_initrd:
                            mods = hidden_mods(TEST_DISK_SIZE)
                            bootstrap_initrd(hidden_initrd, salt, mods)
                            run_qemu(disk_img, hidden_initrd)
    if DEBUG_TRACE_CMDS:
        print("Command trace:")
        for traced in CMD_TRACE:
            print(" ".join(map(str, traced)))
    if SHRED_DEVICE:
        print("Shredding key", shred_key.hex())
    print("Hidden mods:")
    for mod in mods:
        print(mod)
    return 0


if __name__ == "__main__":
    sys.exit(main())
