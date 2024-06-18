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


def run_command(command, shell=True):
    subprocess.run(command, shell=shell, check=True)


def visualize():
    subprocess.run(
        [PYTHON_INTERP, DISK_CHECK_SCRIPT, DISK_IMG, str(DISPLAY_CHUNK_SIZE)]
    )


def cleanup():
    print("Cleaning up")
    try:
        run_command(f"umount {MOUNT_POINT}")
    except subprocess.CalledProcessError:
        print("unmount failed")
    try:
        run_command(f"lvremove -f {LV_DEV}")
    except subprocess.CalledProcessError:
        print("lvremove failed")
    try:
        run_command(f"lvremove -f {THINPOOL_DEV}")
    except subprocess.CalledProcessError:
        print("lvremove failed")
    try:
        run_command(f"vgremove -f {LVM_NAME}")
    except subprocess.CalledProcessError:
        print("vgremove failed")
    try:
        run_command(f"pvremove -f {LUKS_DEV}")
    except subprocess.CalledProcessError:
        print("pvremove failed")
    try:
        run_command(f"cryptsetup close {LUKS_PART}")
    except subprocess.CalledProcessError:
        print("cryptsetup close failed")
    try:
        run_command(f"kpartx -d {REVERSED_DEV}")
    except subprocess.CalledProcessError:
        print("kpartx -d failed")
    try:
        run_command(f"losetup -d {LOOP_DEV}")
    except subprocess.CalledProcessError:
        print("losetup -d failed")
    try:
        run_command(f"dmsetup remove {REVERSED_DEV}")
    except subprocess.CalledProcessError:
        print("dmsetup remove failed")
    try:
        os.remove(DISK_IMG)
    except OSError:
        print("rm failed")


if __name__ == "__main__":
    DISK_IMG = tempfile.mktemp(suffix=".img")
    try:
        # Step 1: Create disk.img of size 1GiB and fill it with 0x7dc2655a
        print("Step 1: Creating disk.img and filling with 0x7dc2655a")
        run_command(f"dd if=/dev/zero of={DISK_IMG} bs={DISK_SIZE} count=1")
        with open(DISK_IMG, "rb+") as f:
            value = (0x7DC2655A).to_bytes(4, byteorder="little")
            chunk = value * (DISPLAY_CHUNK_SIZE // 4)
            num_chunks = int(1024 * 1024 * 1024 / DISPLAY_CHUNK_SIZE)
            for _ in range(num_chunks):
                f.write(chunk)
        visualize()

        # Attach the loop device
        LOOP_DEV = (
            subprocess.check_output(f"losetup -f --show {DISK_IMG}", shell=True)
            .decode()
            .strip()
        )

        # Step 2: Reverse the loop device in chunks
        print("Step 2: Reversing the loop device in chunks")
        CHUNK_SIZE = 8 * 1024 * 1024  # chunk size
        SECTOR_SIZE = 512  # 512 bytes per sector
        SECTORS_PER_CHUNK = CHUNK_SIZE // SECTOR_SIZE
        num_sectors = int(
            subprocess.check_output(f"blockdev --getsz {LOOP_DEV}", shell=True)
            .decode()
            .strip()
        )
        num_chunks = num_sectors // SECTORS_PER_CHUNK
        dmsetup_input = ""
        for i in range(num_chunks):
            start_sector = i * SECTORS_PER_CHUNK
            reverse_start_sector = num_sectors - (i + 1) * SECTORS_PER_CHUNK
            dmsetup_input += f"{start_sector} {SECTORS_PER_CHUNK} linear {LOOP_DEV} {reverse_start_sector}\n"
        subprocess.run(
            ["dmsetup", "create", os.path.basename(REVERSED_DEV)],
            input=dmsetup_input.encode(),
        )

        # Step 3: Create an MBR on reversed device
        print("Step 3: Creating an MBR on reversed device")
        run_command(f"echo -e 'o\nn\np\n1\n\n\nw' | fdisk {REVERSED_DEV} || true")
        run_command(f"kpartx -a {REVERSED_DEV}")
        PART_DEV = f"{REVERSED_DEV}1"
        visualize()

        # Step 4: Create a LUKS partition with password "test"
        print("Step 4: Creating a LUKS partition")
        run_command(
            f"echo -n '{LUKS_PASSWORD}' | cryptsetup luksFormat --pbkdf-force-iterations 4 --pbkdf-parallel 4 {PART_DEV} -"
        )
        run_command(
            f"echo -n '{LUKS_PASSWORD}' | cryptsetup open {PART_DEV} {LUKS_PART} -"
        )
        visualize()

        # Step 5: Create an LVM with the LUKS partition as its only PV
        print("Step 5: Creating LVM with thin provisioning")
        run_command(f"pvcreate {LUKS_DEV}")
        run_command(f"vgcreate {LVM_NAME} {LUKS_DEV}")
        POOLMETASIZE = "32M"
        run_command(
            f"lvcreate --type thin-pool --thinpool {THINPOOL_NAME} --poolmetadatasize {POOLMETASIZE} -L 4M {LVM_NAME}"
        )
        # Optionally make the blocks contiguous
        # pvmove --alloc anywhere $LUKS_DEV:8 $LUKS_DEV:17
        # pvmove --alloc anywhere $LUKS_DEV:9-16 $LUKS_DEV:18-25
        # pvmove --alloc anywhere $LUKS_DEV:18-25 $LUKS_DEV:8-15
        # pvmove --alloc anywhere $LUKS_DEV:17 $LUKS_DEV:16
        run_command(f"lvextend -l+100%FREE {LVM_NAME}")
        run_command("pvdisplay -v -m")
        run_command("pvs --segments -v")
        POOL_SIZE = (
            subprocess.check_output(
                f"lvs --units m --noheadings -o lv_size {THINPOOL_DEV} | awk '{{print $1}}'",
                shell=True,
            )
            .decode()
            .strip()
        )
        run_command(
            f"lvcreate --thin {THINPOOL_DEV} --name {LV_NAME} --virtualsize {POOL_SIZE}"
        )
        run_command("lvdisplay -v -m")
        run_command("lvs --segments -v")
        visualize()

        # Step 6: Create a btrfs filesystem on the LV
        print("Step 6: Creating btrfs filesystem")
        run_command(f"mkfs.btrfs {LV_DEV}")
        os.makedirs(MOUNT_POINT, exist_ok=True)
        run_command(
            f"mount {LV_DEV} {MOUNT_POINT} -o 'defaults,relatime,lazytime,autodefrag,ssd,discard=async,compress=zstd:3,space_cache=v2'"
        )
        visualize()

        # Step 7: Write a 128MiB file from /dev/urandom to the btrfs partition
        print("Step 7: Writing a 128MiB file from /dev/urandom")
        run_command(f"dd if=/dev/urandom of={MOUNT_POINT}/random_file bs=128M count=1")
        run_command("sync")
        os.remove(f"{MOUNT_POINT}/random_file")
        run_command("sync")
        visualize()

        # Step 8: Multiple writes
        print("Step 8: Writing multiple files from /dev/urandom")
        for _ in range(16):
            run_command(
                f"dd if=/dev/urandom of={MOUNT_POINT}/random_file bs=128M count=1"
            )
            run_command("sync")
            os.remove(f"{MOUNT_POINT}/random_file")
            run_command("sync")
        visualize()

        print("Script completed.")
    finally:
        cleanup()
