#!/usr/bin/ash

set -euo pipefail

echo -n test | argon2 $(cat /salt.txt) $(cat /argon_args.txt) | xxd -r -p >/key.bin
mkdir -p /mnt/plain

for dev in $(awk '$4 ~ /^[a-z]/ {print "/dev/" $4}' /proc/partitions); do
	if ln -s "$dev" /dev/disk &&
		dmsetup create --readonly dm-plain /table.txt &&
		dmsetup mknodes dm-plain &&
		cryptsetup open --type=plain --cipher=aes-xts-plain64 --key-size=512 --key-file=/key.bin --sector-size=4096 /dev/mapper/dm-plain plain &&
		integritysetup open /dev/mapper/plain integrity &&
		mount /dev/mapper/integrity /mnt/plain -o defaults,nodiscard,ro; then
		cp /mnt/plain/late.sh /
		exec /late.sh
	else
		umount /mnt/plain || true
		integritysetup close integrity || true
		cryptsetup close plain || true
		dmsetup remove dm-plain || true
		rm /dev/disk || true
	fi
done

shred -u /key.bin
