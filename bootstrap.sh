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
echo '0 1835008 linear /dev/sda 262144' | dmsetup create dm-luks
dmsetup mknodes dm-luks
cryptsetup open --key-file=/key.bin --keyfile-offset=64 --header=/mnt/plain/header.dat /dev/mapper/dm-luks luks
VG=$(vgs --headings none | awk '{print $1;}' | head -n 1)
ln -s /dev/mapper/luks /dev/mapper/$VG-thinpool_tmeta
vgchange -ay --ignoremonitoring -qq $VG
vgmknodes $VG
mkdir /mnt/luks
mount /dev/mapper/$VG-test_lv /mnt/luks -o,defaults,nodiscard,ro
ls /mnt/luks
