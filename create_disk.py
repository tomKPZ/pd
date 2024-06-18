import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager


def cmd(*args, check=True, **kwargs):
    return subprocess.run(*args, check=check, **kwargs)


def cmd_output(*args, **kwargs):
    return cmd(*args, stdout=subprocess.PIPE, **kwargs).stdout.decode().strip()


def visualize(DISK_IMG):
    DISPLAY_CHUNK_SIZE = 1024 * 1024
    PYTHON_INTERP = "pypy3"
    DISK_CHECK_SCRIPT = "disk_check.py"
    subprocess.run(
        [PYTHON_INTERP, DISK_CHECK_SCRIPT, DISK_IMG, str(DISPLAY_CHUNK_SIZE)]
    )


@contextmanager
def disk_image():
    DISK_SIZE = "1G"
    CHUNK_SIZE = 1024 * 1024

    disk_img = tempfile.mktemp(suffix=".img")
    try:
        cmd(["dd", "if=/dev/zero", f"of={disk_img}", f"bs={DISK_SIZE}", "count=1"])
        with open(disk_img, "rb+") as f:
            value = (0x7DC2655A).to_bytes(4, byteorder="little")
            chunk = value * (CHUNK_SIZE // 4)
            num_chunks = int(1024 * 1024 * 1024 / CHUNK_SIZE)
            for _ in range(num_chunks):
                f.write(chunk)
        yield disk_img
    finally:
        os.remove(disk_img)


@contextmanager
def loopback_device(disk_img):
    loop_dev = cmd_output(["losetup", "-f", "--show", disk_img])
    try:
        yield loop_dev
    finally:
        cmd(["losetup", "-d", loop_dev])


@contextmanager
def reversed_device(loop_dev):
    CHUNK_SIZE = 8 * 1024 * 1024
    SECTOR_SIZE = 512
    SECTORS_PER_CHUNK = CHUNK_SIZE // SECTOR_SIZE
    REVERSED_DEV = "/dev/mapper/reversed"

    num_sectors = int(cmd_output(["blockdev", "--getsz", loop_dev]))
    num_chunks = num_sectors // SECTORS_PER_CHUNK
    dmsetup_input = ""
    for i in range(num_chunks):
        start_sector = i * SECTORS_PER_CHUNK
        reverse_start_sector = num_sectors - (i + 1) * SECTORS_PER_CHUNK
        dmsetup_input += f"{start_sector} {SECTORS_PER_CHUNK} linear {loop_dev} {reverse_start_sector}\n"
    cmd(
        ["dmsetup", "create", os.path.basename(REVERSED_DEV)],
        input=dmsetup_input.encode(),
    )
    try:
        yield REVERSED_DEV
    finally:
        cmd(["dmsetup", "remove", REVERSED_DEV])


@contextmanager
def partition(reversed_dev):
    cmd(["fdisk", reversed_dev], input=b"o\nn\np\n1\n\n\nw\n", check=False)
    cmd(["kpartx", "-a", reversed_dev])
    part_dev = f"{reversed_dev}1"
    try:
        yield part_dev
    finally:
        cmd(["kpartx", "-d", reversed_dev])


@contextmanager
def luks(part_dev):
    LUKS_PASSWORD = "test"
    LUKS_PART = "luks_partition"
    LUKS_DEV = f"/dev/mapper/{LUKS_PART}"

    cmd(
        [
            "cryptsetup",
            "luksFormat",
            "--pbkdf-force-iterations",
            "4",
            "--pbkdf-parallel",
            "4",
            part_dev,
        ],
        input=f"{LUKS_PASSWORD}\n".encode(),
    )
    cmd(
        ["cryptsetup", "open", part_dev, LUKS_PART], input=f"{LUKS_PASSWORD}\n".encode()
    )
    try:
        yield LUKS_DEV
    finally:
        cmd(["cryptsetup", "close", LUKS_PART])


@contextmanager
def lvm(luks_dev):
    LV_NAME = "test_lv"
    LVM_NAME = "test_vg"
    THINPOOL_NAME = "thinpool"
    THINPOOL_DEV = f"/dev/{LVM_NAME}/{THINPOOL_NAME}"
    LV_DEV = f"/dev/{LVM_NAME}/{LV_NAME}"

    cmd(["pvcreate", luks_dev])
    cmd(["vgcreate", LVM_NAME, luks_dev])
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
            LVM_NAME,
        ]
    )
    cmd(["lvextend", "-l+100%FREE", LVM_NAME])
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
        cmd(["vgremove", "-f", LVM_NAME], check=False)
        cmd(["pvremove", "-f", luks_dev], check=False)


@contextmanager
def btrfs_mount(lv_dev):
    MOUNT_POINT = "/mnt/test_lv"

    cmd(["mkfs.btrfs", lv_dev])
    os.makedirs(MOUNT_POINT, exist_ok=True)
    cmd(
        [
            "mount",
            lv_dev,
            MOUNT_POINT,
            "-o",
            "defaults,relatime,lazytime,autodefrag,ssd,discard=async,compress=zstd:3,space_cache=v2",
        ]
    )
    try:
        yield MOUNT_POINT
    finally:
        cmd(["umount", MOUNT_POINT], check=False)


def file_test(mount_point):
    cmd(
        [
            "dd",
            "if=/dev/urandom",
            f"of={mount_point}/random_file",
            "bs=128M",
            "count=1",
        ]
    )
    cmd(["sync"])
    os.remove(f"{mount_point}/random_file")
    cmd(["sync"])


def multifile_test(mount_point):
    for _ in range(16):
        cmd(
            [
                "dd",
                "if=/dev/urandom",
                f"of={mount_point}/random_file",
                "bs=128M",
                "count=1",
            ]
        )
        cmd(["sync"])
        os.remove(f"{mount_point}/random_file")
        cmd(["sync"])


def main() -> int:
    with disk_image() as disk_img:
        visualize(disk_img)
        with loopback_device(disk_img) as loop_dev:
            with reversed_device(loop_dev) as reversed_dev:
                with partition(reversed_dev) as part_dev:
                    visualize(disk_img)
                    with luks(part_dev) as luks_dev:
                        visualize(disk_img)
                        with lvm(luks_dev) as lv_dev:
                            visualize(disk_img)
                            with btrfs_mount(lv_dev) as mount_point:
                                visualize(disk_img)
                                file_test(mount_point)
                                visualize(disk_img)
                                multifile_test(mount_point)
                                visualize(disk_img)
                                print("Script completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
