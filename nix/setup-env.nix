{ pkgs, kernel, modulesClosure, plainTree, src, fido2Hmac }:
# A RAM-only setup environment: boot it (as a UKI from a USB stick) on the
# target machine to run `pd randomize`, `pd init-decoy`, `pd init-hidden`.
# Nothing is written anywhere but the target disk, and nothing persists.
let
  inherit (pkgs) lib;

  pdSrc = pkgs.runCommand "pd-src" { } ''
    mkdir -p $out
    cp -r ${src}/pd ${src}/plain ${src}/target $out/
  '';

  # Only the XFS binaries setup needs: the full xfsprogs closure pulls in
  # python3, dbus and ICU through xfs_scrub's helpers (~300 MiB).
  xfsTools = pkgs.runCommand "xfs-tools" { } ''
    mkdir -p $out/bin
    cp ${pkgs.xfsprogs.bin}/bin/mkfs.xfs ${pkgs.xfsprogs.bin}/bin/xfs_repair $out/bin/
  '';

  # Arch Linux's signing keys, so pacstrap verifies what it installs.  The
  # keyring package is pinned by hash (fetched from archive.archlinux.org);
  # bump it now and then.  pacman-key imports keyrings from pacman's own share/.
  archKeyring = pkgs.runCommand "archlinux-keyring" {
    src = pkgs.fetchurl {
      url = "https://archive.archlinux.org/packages/a/archlinux-keyring/archlinux-keyring-20260909-1-any.pkg.tar.zst";
      hash = "sha256-ao6hb1HTswXBgyLViEypWfEsMqPxhocwkP3BhGVgUpM=";
    };
    nativeBuildInputs = [ pkgs.gnutar pkgs.zstd ];
  } ''
    mkdir -p $out
    tar --zstd -xf $src -C $out usr/share/pacman/keyrings
  '';
  pacmanArch = pkgs.pacman.overrideAttrs (old: {
    postInstall = (old.postInstall or "") + ''
      mkdir -p $out/share/pacman/keyrings
      cp ${archKeyring}/usr/share/pacman/keyrings/* $out/share/pacman/keyrings/
    '';
  });
  archInstallScripts = pkgs.arch-install-scripts.override { pacman = pacmanArch; };

  # Arch's own geo-balanced mirror; pacstrap copies the mirror list into the
  # installed system.
  pacmanConf = pkgs.writeText "pacman.conf" ''
    [options]
    Architecture = auto
    SigLevel = Required DatabaseOptional
    LocalFileSigLevel = Optional
    ParallelDownloads = 5

    [core]
    Include = /etc/pacman.d/mirrorlist

    [extra]
    Include = /etc/pacman.d/mirrorlist
  '';
  mirrorlist = pkgs.writeText "mirrorlist" ''
    Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch
  '';

  env = pkgs.buildEnv {
    name = "pd-setup-env";
    paths = with pkgs; [
      python3Minimal
      cryptsetup
      lvm2.bin
      thin-provisioning-tools
      btrfs-progs
      xfsTools
      fido2Hmac
      dosfstools
      util-linux
      coreutils
      findutils
      gnugrep
      gnused
      gawk
      libargon2
      kmod
      xxd
      bashInteractive
      procps
      less
      pciutils
      # Installing the OSes (both need a wired network; see pd-net).
      pacmanArch
      archInstallScripts
      nix
      nixos-install-tools
      gnutar
      iproute2
      (writeShellScriptBin "pd-net" ''
        # Bring up wired networking with DHCP (QEMU, most wired NICs).
        modprobe -q af_packet 2>/dev/null
        for dev in /sys/class/net/*; do
          dev=''${dev##*/}
          [ "$dev" = lo ] && continue
          ip link set "$dev" up
          if /bin/udhcpc -q -n -i "$dev" -s ${udhcpcScript}; then
            # Arch's keys for pacstrap (pacman.conf requires signatures).
            if [ ! -d /etc/pacman.d/gnupg ]; then
              { pacman-key --init && pacman-key --populate archlinux; } >/dev/null 2>&1 ||
                echo "warning: couldn't set up the Arch keyring (pacstrap will fail)" >&2
            fi
            exit 0
          fi
        done
        echo "no DHCP lease on any interface" >&2
        exit 1
      '')
      (writeShellScriptBin "pd" ''
        export PYTHONPATH=${pdSrc} PD_PLAIN_TREE=${plainTree}
        exec ${python3Minimal}/bin/python3 -m pd "$@"
      '')
    ];
    pathsToLink = [ "/bin" "/sbin" "/share" ];
  };

  udhcpcScript = pkgs.writeScript "udhcpc.script" ''
    #!/bin/sh
    case "$1" in
    bound|renew)
      /bin/ip addr flush dev "$interface"
      /bin/ip addr add "$ip/''${mask:-24}" dev "$interface"
      for r in $router; do /bin/ip route add default via "$r" dev "$interface"; break; done
      : > /etc/resolv.conf
      for d in $dns; do echo "nameserver $d" >> /etc/resolv.conf; done
      ;;
    esac
  '';

  lvmConf = pkgs.writeText "lvm.conf" ''
    devices {
      obtain_device_list_from_udev = 0
      external_device_info_source = "none"
    }
    activation {
      udev_sync = 0
      udev_rules = 0
      monitoring = 0
    }
    backup {
      backup = 0
      archive = 0
    }
    global {
      thin_check_executable = "${pkgs.thin-provisioning-tools}/bin/thin_check"
      thin_dump_executable = "${pkgs.thin-provisioning-tools}/bin/thin_dump"
      thin_repair_executable = "${pkgs.thin-provisioning-tools}/bin/thin_repair"
    }
  '';

  init = pkgs.writeText "setup-init" ''
    #!/bin/sh
    export PATH=/bin
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t devtmpfs devtmpfs /dev
    ln -s /proc/self/fd /dev/fd
    ln -s /proc/self/fd/0 /dev/stdin; ln -s /proc/self/fd/1 /dev/stdout; ln -s /proc/self/fd/2 /dev/stderr
    mkdir -p /dev/pts /dev/shm /run /tmp /etc/lvm /mnt
    mount -t devpts devpts /dev/pts
    mount -t tmpfs tmpfs /run
    mount -t tmpfs tmpfs /tmp
    modprobe -a -q dm-mod dm-crypt dm-thin-pool btrfs vfat nls_cp437 nls_iso8859-1 2>/dev/null
    prev=-1
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      find /sys/devices -name modalias -exec cat {} + 2>/dev/null | sort -u |
        xargs -r modprobe -a -q -b 2>/dev/null
      count=$(wc -l </proc/modules)
      [ "$count" -eq "$prev" ] && break
      prev=$count
      sleep 0.5
    done
    hostname localhost
    # Register the store paths shipped in this initrd, for nix and nixos-install.
    ${pkgs.nix}/bin/nix-store --load-db < /etc/nix-registration
    export PATH=${env}/bin:${env}/sbin:/bin HOME=/root TERM=linux LVM_SUPPRESS_FD_WARNINGS=1
    export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
    export PD_SRC=${pdSrc}   # pd's target/ files, for installing OSes
    export NIX_CONFIG="experimental-features = nix-command flakes
    build-users-group =
    sandbox = false"
    mkdir -p /root
    cat <<'EOF'

    pd setup environment (RAM only; nothing here persists)

      pd salt                              print a new salt
      pd randomize   --config pd.toml      fill the disk with random data
      pd init-decoy  --config pd.toml --run
      pd init-hidden --config pd.toml --run
      pd-net                               wired network (DHCP), for installing OSes

    See docs/install.md.  Power off when done (poweroff -f).

    EOF
    cd /root
    setsid cttyhack ${env}/bin/bash -l
    poweroff -f
  '';

  closure = pkgs.closureInfo { rootPaths = [ env pkgs.cacert udhcpcScript ]; };

  initrd = pkgs.runCommand "pd-setup-initrd.cpio.zst" {
    nativeBuildInputs = [ pkgs.cpio pkgs.zstd ];
  } ''
    root=$PWD/root
    mkdir -p $root/{bin,dev,proc,sys,lib,nix/store,etc}
    cp ${pkgs.pkgsStatic.busybox}/bin/busybox $root/bin/
    ln -s bin $root/sbin
    for applet in $($root/bin/busybox --list); do
      [ -e $root/bin/$applet ] || ln -s busybox $root/bin/$applet
    done
    cp -r ${modulesClosure}/lib/modules $root/lib/
    for p in $(cat ${closure}/store-paths); do
      cp -a $p $root/nix/store/
    done
    install -m755 ${init} $root/init
    install -Dm644 ${lvmConf} $root/etc/lvm/lvm.conf
    install -Dm644 ${pacmanConf} $root/etc/pacman.conf
    install -Dm644 ${mirrorlist} $root/etc/pacman.d/mirrorlist
    install -Dm644 ${closure}/registration $root/etc/nix-registration
    chmod -R u+w $root
    (cd $root && find . -print0 | sort -z |
      cpio --null -o -H newc --reproducible --owner=0:0 --quiet) | zstd -10 -T0 -q >$out
  '';

  osRelease = pkgs.writeText "os-release" "NAME=Linux\nID=linux\n";

  uki = cmdline: pkgs.runCommand "pd-setup.efi" { nativeBuildInputs = [ pkgs.systemdUkify ]; } ''
    ukify build --linux ${kernel}/bzImage --initrd ${initrd} \
      --stub ${pkgs.systemd}/lib/systemd/boot/efi/linuxx64.efi.stub \
      --cmdline "${cmdline}" --os-release "@${osRelease}" --output $out
  '';
in
{
  inherit initrd env;
  uki = uki "init_on_free=1 loglevel=4";
}
