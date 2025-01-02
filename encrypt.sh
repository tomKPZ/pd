#!/bin/sh

if [ ! -r /table.txt ]; then
    return 0
fi

modprobe -a -q dm-crypt
dmsetup create dm-luks /table.txt
rm -f /boot_disk
cryptsetup open \
    --key-file=/key.bin \
    --keyfile-offset=64 \
    --header=/header.dat \
    /dev/mapper/dm-luks \
    luks
shred -u /table.txt /header.dat /key.bin
