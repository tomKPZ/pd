#!/bin/bash

set -euo pipefail

# Define variables
DISK_IMG="disk.img"
DISK_SIZE="1G"
CHUNK_SIZE=$((1024 * 1024)) # 1MiB in bytes
LUKS_PASSWORD="test"
LVM_NAME="test_vg"
THINPOOL_NAME="thinpool"
LV_NAME="test_lv"
MOUNT_POINT="/mnt/test_lv"
DISK_CHECK_SCRIPT="disk_check.py"
LOOP_DEV=""

cleanup() {
	echo "Cleaning up"
	sudo umount $MOUNT_POINT || true
	sudo lvremove -f /dev/$LVM_NAME/$LV_NAME || true
	sudo lvremove -f /dev/$LVM_NAME/$THINPOOL_NAME || true
	sudo vgremove -f $LVM_NAME || true
	sudo pvremove -f /dev/mapper/luks_partition || true
	sudo cryptsetup close luks_partition || true
	sudo kpartx -d "$LOOP_DEV" || true
	sudo losetup -d "$LOOP_DEV" || true
}

trap cleanup EXIT

# Step 1: Create disk.img of size 1GiB and fill it with 0x7dc2655a
echo "Step 1: Creating disk.img and filling with 0x7dc2655a"
dd if=/dev/zero of=$DISK_IMG bs=$DISK_SIZE count=1
pypy3 - <<EOF
with open("$DISK_IMG", "rb+") as f:
    value = 0x7dc2655a.to_bytes(4, byteorder='little')
    chunk = value * ($CHUNK_SIZE // 4)
    num_chunks = int(1024 * 1024 * 1024 / $CHUNK_SIZE)
    for _ in range(num_chunks):
        f.write(chunk)
EOF
pypy3 $DISK_CHECK_SCRIPT $DISK_IMG $CHUNK_SIZE

# Step 2: Create an MBR on disk.img
echo "Step 2: Creating an MBR on disk.img"
echo -e "o\nn\np\n1\n\n\nw" | sudo fdisk $DISK_IMG

# Attach the loop device with partitions
LOOP_DEV=$(sudo losetup -f --show $DISK_IMG)
sudo kpartx -a "$LOOP_DEV"

PART_DEV="/dev/mapper/$(basename "$LOOP_DEV")p1"
pypy3 $DISK_CHECK_SCRIPT $DISK_IMG $CHUNK_SIZE

# Step 3: Create a LUKS partition with password "test"
echo "Step 3: Creating a LUKS partition"
echo -n "$LUKS_PASSWORD" | sudo cryptsetup luksFormat \
	--pbkdf-force-iterations 4 \
	--pbkdf-parallel 4 \
	"$PART_DEV" -
echo -n "$LUKS_PASSWORD" | sudo cryptsetup open "$PART_DEV" luks_partition -
pypy3 $DISK_CHECK_SCRIPT $DISK_IMG $CHUNK_SIZE

# Step 4: Create an LVM with the LUKS partition as its only PV
echo "Step 4: Creating LVM with thin provisioning"
sudo pvcreate /dev/mapper/luks_partition
sudo vgcreate $LVM_NAME /dev/mapper/luks_partition
sudo lvcreate \
	--type thin-pool \
	--thinpool $THINPOOL_NAME \
	-l 100%FREE \
	--poolmetadataspare \
	n $LVM_NAME
sudo lvcreate --thin $LVM_NAME/$THINPOOL_NAME --name $LV_NAME --virtualsize 900M
pypy3 $DISK_CHECK_SCRIPT $DISK_IMG $CHUNK_SIZE

# Step 5: Create an ext4 filesystem on the LV
echo "Step 5: Creating ext4 filesystem"
sudo mkfs.btrfs /dev/$LVM_NAME/$LV_NAME
pypy3 $DISK_CHECK_SCRIPT $DISK_IMG $CHUNK_SIZE

# Step 6: Write a 128MiB file from /dev/urandom to the ext4 partition
echo "Step 6: Writing a 128MiB file from /dev/urandom"
sudo mkdir -p $MOUNT_POINT
sudo mount /dev/$LVM_NAME/$LV_NAME $MOUNT_POINT -o \
	'defaults,relatime,lazytime,autodefrag,ssd,discard=async,compress=zstd:3,space_cache=v2'
sudo dd if=/dev/urandom of=$MOUNT_POINT/random_file bs=128M count=1
sudo sync
sudo rm $MOUNT_POINT/random_file
sudo sync
pypy3 $DISK_CHECK_SCRIPT $DISK_IMG $CHUNK_SIZE
sudo umount $MOUNT_POINT

# Step 7: Stress test
echo "Step 7: Writing multiple files from /dev/urandom"
sudo mkdir -p $MOUNT_POINT
sudo mount /dev/$LVM_NAME/$LV_NAME $MOUNT_POINT
for _ in $(seq 1 16); do
	sudo dd if=/dev/urandom of=$MOUNT_POINT/random_file bs=128M count=1
	sudo sync
	sudo rm $MOUNT_POINT/random_file
	sudo sync
done
pypy3 $DISK_CHECK_SCRIPT $DISK_IMG $CHUNK_SIZE
sudo umount $MOUNT_POINT

echo "Script completed."
