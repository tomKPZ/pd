# Kernel modules for the boot environment, plus their dependencies.
# Missing modules are skipped (allowMissing), so the list can be generous.
[
  # Device mapper and crypto
  "dm-mod" "dm-crypt" "dm-thin-pool" "dm-snapshot" "dm-zero"
  "aes" "aesni-intel" "xts" "cbc" "sha256" "sha512" "crc32c" "xxhash64" "blake2b"

  # Filesystems
  "btrfs" "efivarfs" "vfat" "nls-cp437" "nls-iso8859-1"

  # Storage controllers and disks
  "nvme" "vmd" "ahci" "ata-piix" "sata-nv" "sata-via" "sata-sis" "sata-uli"
  "pata-acpi" "ata-generic" "sd-mod" "sr-mod" "virtio-blk" "virtio-scsi"
  "virtio-pci" "virtio-mmio" "mmc-block" "sdhci-pci" "sdhci-acpi" "rtsx-pci-sdmmc"
  "megaraid-sas" "mpt3sas" "hpsa" "aacraid" "isci" "mptspi" "vmw-pvscsi" "hv-storvsc"
  "xen-blkfront"

  # USB host controllers and USB storage
  "xhci-pci" "xhci-plat-hcd" "ehci-pci" "ehci-platform" "ohci-pci" "uhci-hcd"
  "usb-storage" "uas" "typec-ucsi" "ucsi-acpi" "thunderbolt"

  # Keyboards
  "usbhid" "hid-generic" "hid-apple" "hid-asus" "hid-cherry" "hid-corsair"
  "hid-lenovo" "hid-logitech-dj" "hid-logitech-hidpp" "hid-microsoft"
  "hid-multitouch" "hid-roccat" "hid-steelseries" "hid-razer" "hid-holtek-kbd"
  "hid-elan" "hid-ite" "hid-cmedia" "i2c-hid-acpi" "i2c-hid-of" "intel-ish-ipc"
  "intel-ishtp-hid" "amd-sfh" "surface-hid" "surface-aggregator" "atkbd"
  "i8042" "serio-raw" "applespi" "hyperv-keyboard" "virtio-input" "evdev"
  "pinctrl-tigerlake" "pinctrl-alderlake" "pinctrl-meteorlake" "pinctrl-cannonlake"
  "pinctrl-icelake" "pinctrl-sunrisepoint" "pinctrl-amd" "i2c-designware-platform"
  "i2c-designware-pci" "intel-lpss-pci" "intel-lpss-acpi"

  # Display, so the password prompt is visible.
  # (GPU drivers are omitted: their firmware is huge.  The firmware framebuffer
  # is enough for a text console.)
  "simpledrm" "efifb" "bochs"

  # Tests (virtio console, 9p shares)
  "virtio-console" "9p" "9pnet-virtio" "virtio-rng"
]
