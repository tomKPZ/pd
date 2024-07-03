#!/usr/bin/ash

set -euo pipefail

SALT=$(dd if=/dev/sda bs=4K skip=1 count=1 status=none | sha512sum | awk '{print $1;}')
echo -n test | argon2 $SALT -id -t 3 -m 12 -p 1 -l 128 -v 13 -r | xxd -r -p >/key.bin
echo '0 262128 linear /dev/sda 16' | dmsetup create dm-plain
dmsetup mknodes dm-plain
cryptsetup open --type=plain --cipher=aes-xts-plain64 --key-size=512 --key-file=/key.bin --sector-size=4096 /dev/mapper/dm-plain plain
integritysetup open /dev/mapper/plain integrity
mkdir -p /mnt/plain
mount /dev/mapper/integrity /mnt/plain -o,defaults,nodiscard,ro
exec /mnt/plain/late.sh
