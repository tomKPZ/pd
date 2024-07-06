#!/usr/bin/ash

set -euo pipefail

echo -n test | argon2 $(cat /salt.txt) $(cat /argon_args.txt) | xxd -r -p >/key.bin
mkdir -p /mnt/plain

for dev in $(cat /proc/partitions | tail -n +3 | awk '{print "/dev/"$4;}'); do
	ln -s "$dev" /dev/disk
	if dmsetup create --readonly dm-plain /table.txt; then
		dmsetup mknodes dm-plain
		if cryptsetup open --type=plain --cipher=aes-xts-plain64 --key-size=512 --key-file=/key.bin --sector-size=4096 /dev/mapper/dm-plain plain; then
			if integritysetup open /dev/mapper/plain integrity; then
				if mount /dev/mapper/integrity /mnt/plain -o defaults,nodiscard,ro; then
					cp /mnt/plain/late.sh /
					exec /late.sh
				fi
				integritysetup close integrity
			fi
			cryptsetup close plain
		fi
		dmsetup remove dm-plain
	fi
	rm /dev/disk
done

shred -u /key.bin
