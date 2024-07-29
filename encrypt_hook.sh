#!/usr/bin/ash

run_hook() {
    modprobe -a -q dm-crypt
    dmsetup create dm-luks /table.txt
    rm /boot_disk
    cryptsetup open --key-file=/key.bin --keyfile-offset=64 --header=/header.dat /dev/mapper/dm-luks luks
    shred -u /table.txt /header.dat /key.bin
}
