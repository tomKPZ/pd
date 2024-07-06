#!/usr/bin/ash

set -euo pipefail

ln -s /dev/sda /dev/disk
echo -n test | argon2 $(cat /salt.txt) $(cat /argon_args.txt) | xxd -r -p >/key.bin
dmsetup create --readonly dm-plain /table.txt
dmsetup mknodes dm-plain
cryptsetup open --type=plain --cipher=aes-xts-plain64 --key-size=512 --key-file=/key.bin --sector-size=4096 /dev/mapper/dm-plain plain
integritysetup open /dev/mapper/plain integrity
mkdir /mnt/plain
mount /dev/mapper/integrity /mnt/plain -o,defaults,nodiscard,ro
exec /mnt/plain/late.sh
