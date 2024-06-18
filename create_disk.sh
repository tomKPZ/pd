#!/bin/bash

set -euo pipefail

# Define variables
DISK_IMG="$(mktemp --suffix=.img)"
DISK_SIZE="1G"
DISPLAY_CHUNK_SIZE=$((1024 * 1024)) # 1MiB in bytes
LUKS_PASSWORD="test"
LUKS_PART="luks_partition"
LUKS_DEV="/dev/mapper/$LUKS_PART"
LVM_NAME="test_vg"
THINPOOL_NAME="thinpool"
THINPOOL_DEV="/dev/$LVM_NAME/$THINPOOL_NAME"
LV_NAME="test_lv"
LV_DEV="/dev/$LVM_NAME/$LV_NAME"
MOUNT_POINT="/mnt/test_lv"
DISK_CHECK_SCRIPT="disk_check.py"
REVERSED_DEV="/dev/mapper/reversed"
LOOP_DEV=""

cleanup() {
	echo "Cleaning up"
	umount $MOUNT_POINT || echo "unmount failed"
	lvremove -f $LV_DEV || echo "lvremove failed"
	lvremove -f $THINPOOL_DEV || echo "lvremove failed"
	vgremove -f $LVM_NAME || echo "vgremove failed"
	pvremove -f $LUKS_DEV || echo "pvremove failed"
	cryptsetup close $LUKS_PART || echo "cryptsetup close failed"
	kpartx -d "$REVERSED_DEV" || echo "kpartx -d failed"
	losetup -d "$LOOP_DEV" || echo "losetup -d failed"
	dmsetup remove "$REVERSED_DEV" || echo "dmsetup remove failed"
	rm "$DISK_IMG" || echo "rm failed"
}

visualize() {
	sync
	pypy3 $DISK_CHECK_SCRIPT "$DISK_IMG" $DISPLAY_CHUNK_SIZE
}

trap cleanup EXIT

# Step 1: Create disk.img of size 1GiB and fill it with 0x7dc2655a
echo "Step 1: Creating disk.img and filling with 0x7dc2655a"
dd if=/dev/zero of="$DISK_IMG" bs=$DISK_SIZE count=1
pypy3 - <<EOF
with open("$DISK_IMG", "rb+") as f:
    value = 0x7dc2655a.to_bytes(4, byteorder='little')
    chunk = value * ($DISPLAY_CHUNK_SIZE // 4)
    num_chunks = int(1024 * 1024 * 1024 / $DISPLAY_CHUNK_SIZE)
    for _ in range(num_chunks):
        f.write(chunk)
EOF
visualize

# Attach the loop device
LOOP_DEV=$(losetup -f --show "$DISK_IMG")

# Step 2: Reverse the loop device in chunks
echo "Step 2: Reversing the loop device in chunks of 64MiB"
CHUNK_SIZE=$((8 * 1024 * 1024)) # chunk size
SECTOR_SIZE=512                 # 512 bytes per sector
SECTORS_PER_CHUNK=$((CHUNK_SIZE / SECTOR_SIZE))
num_sectors=$(blockdev --getsz "$LOOP_DEV")
num_chunks=$((num_sectors / SECTORS_PER_CHUNK))
{
	for ((i = 0; i < num_chunks; i++)); do
		start_sector=$((i * SECTORS_PER_CHUNK))
		reverse_start_sector=$((num_sectors - (i + 1) * SECTORS_PER_CHUNK))
		echo "${start_sector} ${SECTORS_PER_CHUNK} linear $LOOP_DEV ${reverse_start_sector}"
	done
} | dmsetup create "$(basename $REVERSED_DEV)"

# Step 3: Create an MBR on reversed device
echo "Step 3: Creating an MBR on reversed device"
# fdisk has a non-zero exit code to warn to run partx.
echo -e "o\nn\np\n1\n\n\nw" | fdisk $REVERSED_DEV || true
# Create a loop device with partitions
kpartx -a "$REVERSED_DEV"
PART_DEV="${REVERSED_DEV}1"
visualize

# Step 4: Create a LUKS partition with password "test"
echo "Step 4: Creating a LUKS partition"
echo -n "$LUKS_PASSWORD" | cryptsetup luksFormat \
	--pbkdf-force-iterations 4 \
	--pbkdf-parallel 4 \
	"$PART_DEV" -
echo -n "$LUKS_PASSWORD" | cryptsetup open "$PART_DEV" $LUKS_PART -
visualize

# Step 5: Create an LVM with the LUKS partition as its only PV
echo "Step 5: Creating LVM with thin provisioning"
pvcreate $LUKS_DEV
vgcreate $LVM_NAME $LUKS_DEV
# 32M -> 2G for 2TB?
POOLMETASIZE=32M
lvcreate \
	--type thin-pool \
	--thinpool $THINPOOL_NAME \
	--poolmetadatasize ${POOLMETASIZE} \
	-L 4M \
	$LVM_NAME
# Optionally make the blocks contiguous
# pvmove --alloc anywhere $LUKS_DEV:8 $LUKS_DEV:17
# pvmove --alloc anywhere $LUKS_DEV:9-16 $LUKS_DEV:18-25
# pvmove --alloc anywhere $LUKS_DEV:18-25 $LUKS_DEV:8-15
# pvmove --alloc anywhere $LUKS_DEV:17 $LUKS_DEV:16
lvextend -l+100%FREE $LVM_NAME
pvdisplay -v -m
pvs --segments -v
POOL_SIZE=$(lvs --units m --noheadings -o lv_size $THINPOOL_DEV | awk '{print $1}')
lvcreate --thin $THINPOOL_DEV --name $LV_NAME --virtualsize "$POOL_SIZE"
lvdisplay -v -m
lvs --segments -v
visualize

# Step 6: Create an btrfs filesystem on the LV
echo "Step 6: Creating btrfs filesystem"
mkfs.btrfs $LV_DEV
mkdir -p $MOUNT_POINT
mount $LV_DEV $MOUNT_POINT -o \
	'defaults,relatime,lazytime,autodefrag,ssd,discard=async,compress=zstd:3,space_cache=v2'
visualize

# Step 7: Write a 128MiB file from /dev/urandom to the btrfs partition
echo "Step 7: Writing a 128MiB file from /dev/urandom"
dd if=/dev/urandom of=$MOUNT_POINT/random_file bs=128M count=1
sync
rm $MOUNT_POINT/random_file
sync
visualize

# Step 8: Multiple writes
echo "Step 8: Writing multiple files from /dev/urandom"
for _ in $(seq 1 16); do
	dd if=/dev/urandom of=$MOUNT_POINT/random_file bs=128M count=1
	sync
	rm $MOUNT_POINT/random_file
	sync
done
visualize

echo "Script completed."
