import os
import subprocess
import tempfile

DISK_SIZE = "1G"
LUKS_PASSWORD = "test"
LUKS_PART = "luks_partition"
LUKS_DEV = f"/dev/mapper/{LUKS_PART}"
LVM_NAME = "test_vg"
THINPOOL_NAME = "thinpool"
THINPOOL_DEV = f"/dev/{LVM_NAME}/{THINPOOL_NAME}"
LV_NAME = "test_lv"
LV_DEV = f"/dev/{LVM_NAME}/{LV_NAME}"
MOUNT_POINT = "/mnt/test_lv"
REVERSED_DEV = "/dev/mapper/reversed"
DISPLAY_CHUNK_SIZE = 1024 * 1024
PYTHON_INTERP = "pypy3"
DISK_CHECK_SCRIPT = "disk_check.py"


def cmd(*args, check=True, **kwargs):
    return subprocess.run(*args, check=check, **kwargs)


def cmd_output(*args, **kwargs):
    return cmd(*args, stdout=subprocess.PIPE, **kwargs).stdout.decode().strip()


def visualize():
    subprocess.run(
        [PYTHON_INTERP, DISK_CHECK_SCRIPT, DISK_IMG, str(DISPLAY_CHUNK_SIZE)]
    )


def cleanup():
    print("Cleaning up")
    cmd(["umount", MOUNT_POINT], check=False)
    cmd(["lvremove", "-f", LV_DEV], check=False)
    cmd(["lvremove", "-f", THINPOOL_DEV], check=False)
    cmd(["vgremove", "-f", LVM_NAME], check=False)
    cmd(["pvremove", "-f", LUKS_DEV], check=False)
    cmd(["cryptsetup", "close", LUKS_PART], check=False)
    cmd(["kpartx", "-d", REVERSED_DEV], check=False)
    cmd(["losetup", "-d", LOOP_DEV], check=False)
    cmd(["dmsetup", "remove", REVERSED_DEV], check=False)
    os.remove(DISK_IMG)


if __name__ == "__main__":
    DISK_IMG = tempfile.mktemp(suffix=".img")
    try:
        # Step 1: Create disk.img of size 1GiB and fill it with 0x7dc2655a
        print("Step 1: Creating disk.img and filling with 0x7dc2655a")
        cmd(["dd", "if=/dev/zero", f"of={DISK_IMG}", "bs=1G", "count=1"])
        with open(DISK_IMG, "rb+") as f:
            value = (0x7DC2655A).to_bytes(4, byteorder="little")
            chunk = value * (DISPLAY_CHUNK_SIZE // 4)
            num_chunks = int(1024 * 1024 * 1024 / DISPLAY_CHUNK_SIZE)
            for _ in range(num_chunks):
                f.write(chunk)
        visualize()

        # Attach the loop device
        LOOP_DEV = cmd_output(["losetup", "-f", "--show", DISK_IMG])

        # Step 2: Reverse the loop device in chunks
        print("Step 2: Reversing the loop device in chunks")
        CHUNK_SIZE = 8 * 1024 * 1024  # chunk size
        SECTOR_SIZE = 512  # 512 bytes per sector
        SECTORS_PER_CHUNK = CHUNK_SIZE // SECTOR_SIZE
        num_sectors = int(cmd_output(["blockdev", "--getsz", LOOP_DEV]))
        num_chunks = num_sectors // SECTORS_PER_CHUNK
        dmsetup_input = ""
        for i in range(num_chunks):
            start_sector = i * SECTORS_PER_CHUNK
            reverse_start_sector = num_sectors - (i + 1) * SECTORS_PER_CHUNK
            dmsetup_input += f"{start_sector} {SECTORS_PER_CHUNK} linear {LOOP_DEV} {reverse_start_sector}\n"
        cmd(
            ["dmsetup", "create", os.path.basename(REVERSED_DEV)],
            input=dmsetup_input.encode(),
        )

        # Step 3: Create an MBR on reversed device
        print("Step 3: Creating an MBR on reversed device")
        cmd(["fdisk", REVERSED_DEV], input=b"o\nn\np\n1\n\n\nw\n", check=False)
        cmd(["kpartx", "-a", REVERSED_DEV])
        PART_DEV = f"{REVERSED_DEV}1"
        visualize()

        # Step 4: Create a LUKS partition with password "test"
        print("Step 4: Creating a LUKS partition")
        cmd(
            [
                "cryptsetup",
                "luksFormat",
                "--pbkdf-force-iterations",
                "4",
                "--pbkdf-parallel",
                "4",
                PART_DEV,
            ],
            input=f"{LUKS_PASSWORD}\n".encode(),
        )
        cmd(
            ["cryptsetup", "open", PART_DEV, LUKS_PART],
            input=f"{LUKS_PASSWORD}\n".encode(),
        )
        visualize()

        # Step 5: Create an LVM with the LUKS partition as its only PV
        print("Step 5: Creating LVM with thin provisioning")
        cmd(["pvcreate", LUKS_DEV])
        cmd(["vgcreate", LVM_NAME, LUKS_DEV])
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
        cmd(["pvdisplay", "-v", "-m"])
        cmd(["pvs", "--segments", "-v"])
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
        cmd(["lvdisplay", "-v", "-m"])
        cmd(["lvs", "--segments", "-v"])
        visualize()

        # Step 6: Create a btrfs filesystem on the LV
        print("Step 6: Creating btrfs filesystem")
        cmd(["mkfs.btrfs", LV_DEV])
        os.makedirs(MOUNT_POINT, exist_ok=True)
        cmd(
            [
                "mount",
                LV_DEV,
                MOUNT_POINT,
                "-o",
                "defaults,relatime,lazytime,autodefrag,ssd,discard=async,compress=zstd:3,space_cache=v2",
            ]
        )
        visualize()

        # Step 7: Write a 128MiB file from /dev/urandom to the btrfs partition
        print("Step 7: Writing a 128MiB file from /dev/urandom")
        cmd(
            [
                "dd",
                "if=/dev/urandom",
                f"of={MOUNT_POINT}/random_file",
                "bs=128M",
                "count=1",
            ]
        )
        cmd(["sync"])
        os.remove(f"{MOUNT_POINT}/random_file")
        cmd(["sync"])
        visualize()

        # Step 8: Multiple writes
        print("Step 8: Writing multiple files from /dev/urandom")
        for _ in range(16):
            cmd(
                [
                    "dd",
                    "if=/dev/urandom",
                    f"of={MOUNT_POINT}/random_file",
                    "bs=128M",
                    "count=1",
                ]
            )
            cmd(["sync"])
            os.remove(f"{MOUNT_POINT}/random_file")
            cmd(["sync"])
        visualize()

        print("Script completed.")
    finally:
        cleanup()
