import contextlib
import functools
import os
import subprocess
import sys
import tempfile
import uuid


def cmd(*args, check=True, **kwargs):
    return subprocess.run(*args, check=check, **kwargs)


def cmd_output(*args, **kwargs):
    return cmd(*args, stdout=subprocess.PIPE, **kwargs).stdout.decode().strip()


def batched(xs, n):
    assert len(xs) % n == 0
    return [xs[i * n : i * n + n] for i in range(len(xs) // n)]


def visualize(device):
    chunk_size = 1024 * 1024
    chunks_per_line = 64
    chunk_expect = b"\0" * chunk_size

    data = cmd(["dd", f"if={device}"], stdout=subprocess.PIPE).stdout
    changes = [chunk != chunk_expect for chunk in batched(data, chunk_size)]
    for line in batched(changes, chunks_per_line):
        for change in line:
            code = [42, 41][change]
            print(f"\033[{code}m \033[0m", end="")
        print()


@contextlib.contextmanager
def dummy_file():
    file = tempfile.mktemp()
    try:
        cmd(["dd", "if=/dev/urandom", f"of={file}", "bs=4M", "count=32"])
        yield file
    finally:
        os.remove(file)


@contextlib.contextmanager
def disk_image():
    DISK_SIZE = "1G"

    disk_img = tempfile.mktemp(suffix=".img")
    try:
        cmd(["dd", "if=/dev/zero", f"of={disk_img}", f"bs={DISK_SIZE}", "count=1"])
        yield disk_img
    finally:
        os.remove(disk_img)


@contextlib.contextmanager
def loopback_device(file_path):
    loop_dev = cmd_output(["losetup", "-f", "--show", file_path])
    try:
        yield loop_dev
    finally:
        cmd(["losetup", "-d", loop_dev])


@contextlib.contextmanager
def reversed_device(reverse, wrap_dev):
    if not reverse:
        yield wrap_dev
        return
    CHUNK_SIZE = 8 * 1024 * 1024
    SECTOR_SIZE = 512
    SECTORS_PER_CHUNK = CHUNK_SIZE // SECTOR_SIZE
    REVERSED_NAME = f"reversed_{uuid.uuid4()}"
    REVERSED_DEV = f"/dev/mapper/{REVERSED_NAME}"

    num_sectors = int(cmd_output(["blockdev", "--getsz", wrap_dev]))
    num_chunks = num_sectors // SECTORS_PER_CHUNK
    dmsetup_input = ""
    for i in range(num_chunks):
        start_sector = i * SECTORS_PER_CHUNK
        reverse_start_sector = num_sectors - (i + 1) * SECTORS_PER_CHUNK
        dmsetup_input += f"{start_sector} {SECTORS_PER_CHUNK} linear {wrap_dev} {reverse_start_sector}\n"
    cmd(
        ["dmsetup", "create", os.path.basename(REVERSED_DEV)],
        input=dmsetup_input.encode(),
    )
    try:
        yield REVERSED_DEV
    finally:
        cmd(["dmsetup", "remove", REVERSED_DEV])


@contextlib.contextmanager
def partition(wrap_dev):
    cmd(["fdisk", wrap_dev], input=b"o\nn\np\n1\n\n\nw\n", check=False)
    name = cmd_output(["kpartx", "-l", wrap_dev]).split()[0]
    cmd(["kpartx", "-a", wrap_dev])
    part_dev = f"/dev/mapper/{name}"
    try:
        yield part_dev
    finally:
        cmd(["kpartx", "-d", wrap_dev])


@contextlib.contextmanager
def luks(wrap_dev):
    LUKS_PASSWORD = "test"
    LUKS_NAME = f"luks_test_{uuid.uuid4()}"
    LUKS_DEV = f"/dev/mapper/{LUKS_NAME}"

    cmd(
        [
            "cryptsetup",
            "luksFormat",
            "--pbkdf-force-iterations",
            "4",
            "--pbkdf-parallel",
            "4",
            wrap_dev,
        ],
        input=f"{LUKS_PASSWORD}\n".encode(),
    )
    cmd(
        ["cryptsetup", "open", wrap_dev, LUKS_NAME], input=f"{LUKS_PASSWORD}\n".encode()
    )
    try:
        yield LUKS_DEV
    finally:
        cmd(["cryptsetup", "close", LUKS_NAME])


@contextlib.contextmanager
def plain_dmcrypt(wrap_dev):
    CRYPT_PASSWORD = "test"
    CRYPT_NAME = f"dmcrypt_test_{uuid.uuid4()}"
    CRYPT_DEV = f"/dev/mapper/{CRYPT_NAME}"

    cmd(
        ["cryptsetup", "open", "--type", "plain", wrap_dev, CRYPT_NAME],
        input=f"{CRYPT_PASSWORD}\n".encode(),
    )
    try:
        yield CRYPT_DEV
    finally:
        cmd(["cryptsetup", "close", CRYPT_NAME])


@contextlib.contextmanager
def integrity(wrap_dev):
    INTEGRITY_NAME = f"integrity_test_{uuid.uuid4()}"
    INTEGRITY_DEV = f"/dev/mapper/{INTEGRITY_NAME}"

    cmd(
        [
            "integritysetup",
            "format",
            "--batch-mode",
            "--integrity-no-journal",
            "--sector-size=4096",
            wrap_dev,
        ]
    )
    cmd(["integritysetup", "open", wrap_dev, INTEGRITY_NAME])
    try:
        yield INTEGRITY_DEV
    finally:
        cmd(["integritysetup", "close", INTEGRITY_NAME])


@contextlib.contextmanager
def lvm(wrap_dev):
    LV_NAME = "test_lv"
    VG_NAME = f"test_vg_{uuid.uuid4()}"
    THINPOOL_NAME = "thinpool"
    THINPOOL_DEV = f"/dev/{VG_NAME}/{THINPOOL_NAME}"
    LV_DEV = f"/dev/{VG_NAME}/{LV_NAME}"

    cmd(["pvcreate", wrap_dev])
    cmd(["vgcreate", VG_NAME, wrap_dev])
    POOLMETASIZE = "32M"
    cmd(
        [
            "lvcreate",
            "--type",
            "thin-pool",
            "--thinpool",
            THINPOOL_NAME,
            "--poolmetadatasize",
            POOLMETASIZE,
            "-L",
            "4M",
            VG_NAME,
        ]
    )
    cmd(["lvextend", "-l+100%FREE", VG_NAME])
    POOL_SIZE = cmd_output(
        ["lvs", "--units", "m", "--noheadings", "-o", "lv_size", THINPOOL_DEV]
    ).split()[0]
    cmd(
        [
            "lvcreate",
            "--thin",
            THINPOOL_DEV,
            "--name",
            LV_NAME,
            "--virtualsize",
            POOL_SIZE,
        ]
    )
    try:
        yield LV_DEV
    finally:
        cmd(["lvremove", "-f", LV_DEV], check=False)
        cmd(["lvremove", "-f", THINPOOL_DEV], check=False)
        cmd(["vgremove", "-f", VG_NAME], check=False)
        cmd(["pvremove", "-f", wrap_dev], check=False)


@contextlib.contextmanager
def btrfs_mount(wrap_dev):
    MOUNT_NAME = f"btrfs_mount_{uuid.uuid4()}"
    MOUNT_POINT = f"/mnt/{MOUNT_NAME}"

    cmd(["mkfs.btrfs", wrap_dev, "--csum=xxhash"])
    os.mkdir(MOUNT_POINT)
    cmd(
        [
            "mount",
            wrap_dev,
            MOUNT_POINT,
            "-o",
            "defaults,relatime,lazytime,autodefrag,ssd,discard=async,compress=zstd:3,space_cache=v2",
        ]
    )
    try:
        yield MOUNT_POINT
    finally:
        cmd(["umount", MOUNT_POINT], check=False)
        os.rmdir(MOUNT_POINT)


def file_test(mount_point, file_path):
    cmd(["dd", f"if={file_path}", f"of={mount_point}/dummy", "bs=4M"])
    cmd(["sync"])
    os.remove(f"{mount_point}/dummy")
    cmd(["sync"])


def multifile_test(mount_point, file_path):
    for _ in range(16):
        file_test(mount_point, file_path)


@contextlib.contextmanager
def manage(device, managers, i, prev):
    if i == len(managers):
        yield prev
        return
    manager = managers[i]
    print(f"Step {i + 1}: {manager}")
    with manager(prev) as managed:
        visualize(device)
        with manage(device, managers, i + 1, managed) as res:
            yield res


def managers(reverse):
    return [
        loopback_device,
        functools.partial(reversed_device, reverse),
        partition,
        luks,
        lvm,
        btrfs_mount,
    ]


def main() -> int:
    with disk_image() as disk_img:
        with plain_dmcrypt(disk_img) as plain:
            cmd(["dd", "if=/dev/zero", f"of={plain}", f"bs=4M", "count=256"])
            with manage(plain, managers(False), 0, disk_img) as mount1:
                with manage(plain, managers(True), 0, disk_img) as mount2:
                    with dummy_file() as dummy:
                        file_test(mount1, dummy)
                        file_test(mount2, dummy)
                        visualize(plain)
                        multifile_test(mount1, dummy)
                        multifile_test(mount2, dummy)
                        visualize(plain)
                        print("Script completed.")
                        return 0


if __name__ == "__main__":
    sys.exit(main())
