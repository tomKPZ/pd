#!/usr/bin/ash

set -euo pipefail

ln -s /dev/nvme0n1 /dev/disk
dmsetup create dm-luks /table.txt
dmsetup mknodes dm-luks
cryptsetup open --key-file=/key.bin --keyfile-offset=64 --header=/header.dat /dev/mapper/dm-luks luks
shred -u /table.txt /header.dat /key.bin
VG=$(vgs --headings none | awk '{print $1;}' | head -n 1)
ln -s /dev/mapper/luks /dev/mapper/$VG-thinpool_tmeta
vgchange -ay --ignoremonitoring -qq $VG
vgmknodes $VG

mkdir /mnt/luks
mount /dev/mapper/$VG-test_lv /mnt/luks -o,defaults,nodiscard,ro
cat /mnt/luks/message.txt
