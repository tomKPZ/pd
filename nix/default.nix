{ pkgs, src }:
let
  inherit (pkgs) lib;
  static = pkgs.pkgsStatic;

  lvm2Static = (static.lvm2.override {
    onlyLib = false;
    udevSupport = false;
  }).overrideAttrs (old: {
    # Static builds produce no shared library for the "lib" output.
    postInstall = (old.postInstall or "") + ''
      mkdir -p $lib
    '';
  });

  detectShift = static.stdenv.mkDerivation {
    pname = "detect_shift";
    version = "1";
    src = src + "/tools";
    dontConfigure = true;
    buildPhase = ''
      $CC -static -O2 -Wall -Wextra -Werror detect_shift.c -o detect_shift
    '';
    installPhase = ''
      install -Dm755 detect_shift $out/bin/detect_shift
    '';
  };

  # USB only: no smartcard (pcsc) support, whose static build fails anyway.
  # Only the library: libfido2's own tests and tools don't link statically.
  # libcbor's static build uses LTO, whose archive index has no symbols.
  libcborStatic = static.libcbor.overrideAttrs (old: {
    env = (old.env or { }) // { NIX_CFLAGS_COMPILE = "-fno-lto"; };
  });
  libfido2Static = (static.libfido2.override {
    withPcsclite = false;
    libcbor = libcborStatic;
  }).overrideAttrs (old: {
    cmakeFlags = old.cmakeFlags ++ [
      "-DBUILD_TESTS=OFF" "-DBUILD_EXAMPLES=OFF" "-DBUILD_TOOLS=OFF" "-DBUILD_MANPAGES=OFF"
    ];
    outputs = [ "out" "dev" ];
    doInstallCheck = false;
  });

  # FIDO2 hmac-secret for the boot USB and for setup (docs/design.md §4.1).
  fido2Hmac = static.stdenv.mkDerivation {
    pname = "fido2-hmac";
    version = "1";
    src = src + "/tools";
    nativeBuildInputs = [ pkgs.pkg-config ];
    buildInputs = [ libfido2Static libcborStatic static.libudev-zero static.openssl static.zlib ];
    dontConfigure = true;
    # libfido2.pc doesn't list its private dependencies.
    buildPhase = ''
      $CC -static -O2 -Wall -Wextra -Werror fido2_hmac.c -o fido2-hmac \
        $(pkg-config --cflags libfido2) -lfido2 -lcbor -ludev -lcrypto -lz -lpthread
    '';
    installPhase = ''
      install -Dm755 fido2-hmac $out/bin/fido2-hmac
    '';
  };

  # Copy statically linked binaries into one bin/ directory, stripped, and
  # refuse anything that is dynamically linked.
  staticBin = name: bins: pkgs.runCommand name { nativeBuildInputs = [ pkgs.binutils pkgs.file ]; } ''
    mkdir -p $out/bin
    ${lib.concatMapStrings (b: ''
      cp -L ${b} $out/bin/$(basename ${b} .static)
    '') bins}
    chmod -R u+w $out/bin
    for f in $out/bin/*; do
      strip --strip-all "$f"
      if file "$f" | grep -q 'dynamically linked'; then
        echo "$f is dynamically linked" >&2
        exit 1
      fi
    done
  '';

  # Tools on the boot USB.  Keep this list minimal: anything here is visible
  # to someone who has the USB but no password.
  bootTools = staticBin "pd-boot-tools" [
    "${static.busybox}/bin/busybox"
    "${lib.getBin static.cryptsetup}/bin/cryptsetup"
    "${static.libargon2}/bin/argon2"
    "${fido2Hmac}/bin/fido2-hmac"
  ];

  # Tools stored in the encrypted boot volume, used by late.sh.
  plainTools = staticBin "pd-plain-tools" [
    "${lvm2Static}/sbin/lvm.static"
    "${lvm2Static}/sbin/dmsetup.static"
    "${static.thin-provisioning-tools}/bin/pdata_tools"
    "${static.kexec-tools}/bin/kexec"
    "${lib.getBin static.jq}/bin/jq"
    "${static.fzf}/bin/fzf"
    "${detectShift}/bin/detect_shift"
    "${static.busybox}/bin/busybox"
  ];

  kernel = pkgs.linuxPackages.kernel;

  # Broad hardware coverage: a missing keyboard or storage driver means a
  # machine that cannot boot.
  rootModules = import ./modules.nix;

  # busybox modprobe can't reliably load compressed modules, so decompress
  # them and regenerate the dependency files.
  mkModulesClosure = rootModules: let
    closure = pkgs.makeModulesClosure {
      kernel = pkgs.aggregateModules [ kernel.modules ];
      firmware = pkgs.linux-firmware;
      inherit rootModules;
      allowMissing = true;
    };
  in pkgs.runCommand "pd-modules" { nativeBuildInputs = [ pkgs.xz pkgs.kmod ]; } ''
    cp -r ${closure} $out
    chmod -R u+w $out
    find $out/lib/modules -name '*.ko.xz' -exec xz -d {} +
    ver=$(ls $out/lib/modules)
    depmod -b $out -a $ver
  '';
  modulesClosure = mkModulesClosure rootModules;
  # Wired network drivers, so `pd-net` works on real machines (installing the
  # OSes needs a network).  Setup UKI only: the boot USB never networks.
  nicModules = [
    "e1000e" "igb" "igc" "e1000" "r8169" "tg3" "bnx2" "alx" "atl1c" "atlantic" "sky2" "ixgbe"
    # USB and USB-C Ethernet adapters and docks
    "r8152" "cdc-ether" "cdc-ncm" "ax88179-178a" "asix" "usbnet"
  ];
  # Setup formats extra volumes as XFS; the boot environment never mounts them.
  setupModulesClosure = mkModulesClosure (rootModules ++ [ "xfs" ] ++ nicModules);
  # The tool VM also has ext4 and f2fs, for the filesystem survey.
  toolModulesClosure = mkModulesClosure (rootModules ++ [ "xfs" "ext4" "f2fs" ]);

  # The boot environment's initrd, without per-install files (salt and
  # early_env.sh, which `pd usb` appends as a second cpio).
  bootInitrd = pkgs.runCommand "pd-boot-initrd.cpio.zst" {
    nativeBuildInputs = [ pkgs.cpio pkgs.zstd pkgs.findutils ];
  } ''
    root=$PWD/root
    mkdir -p $root/{bin,dev,proc,sys,run,tmp,mnt,etc,lib}
    cp ${bootTools}/bin/* $root/bin/
    # The kernel runs /sbin/modprobe to load crypto algorithms on demand.
    ln -s bin $root/sbin
    for applet in $($root/bin/busybox --list); do
      [ -e $root/bin/$applet ] || ln -s busybox $root/bin/$applet
    done
    cp -r ${modulesClosure}/lib/modules $root/lib/
    if [ -d ${modulesClosure}/lib/firmware ]; then
      cp -r ${modulesClosure}/lib/firmware $root/lib/
    fi
    install -m755 ${src}/boot/init $root/init
    install -m755 ${src}/boot/early.sh $root/early.sh
    chmod -R u+w $root
    (cd $root && find . -print0 | sort -z |
      cpio --null -o -H newc --reproducible --owner=0:0 --quiet) |
      zstd -19 -T0 -q >$out
  '';

  # Contents of the encrypted boot volume, minus per-install files.
  plainTree = pkgs.runCommand "pd-plain-tree" { } ''
    mkdir -p $out/bin
    cp ${plainTools}/bin/* $out/bin/
    ln -s pdata_tools $out/bin/thin_check
    ln -s pdata_tools $out/bin/thin_dump
    ln -s pdata_tools $out/bin/thin_rmap
    # The scripts' userland, so they run the same on pd's USB and on a live
    # system (docs/stock-boot.md), whatever awk or cpio that has.
    for a in awk basename blockdev cat chmod cp cpio cut dd dirname find grep ls mkdir mount \
        readlink rm sed shred sort stat tr; do
      ln -s busybox $out/bin/$a
    done
    install -m755 ${src}/plain/late.sh $out/late.sh
    install -m644 ${src}/plain/thin-ro.sh $out/thin-ro.sh
    cp -r ${src}/plain/systems $out/systems
  '';
  # Console keymaps in the binary format busybox loadkmap reads, one per
  # kbd keymap (`pd usb` puts the configured one on the USB).
  keymaps = pkgs.runCommand "pd-keymaps" { nativeBuildInputs = [ pkgs.kbd ]; } ''
    mkdir -p $out
    for f in ${pkgs.kbd}/share/keymaps/i386/*/*.map.gz; do
      name=$(basename "$f" .map.gz)
      loadkeys -b "$f" > "$out/$name.bmap" 2>/dev/null </dev/null || rm -f "$out/$name.bmap"
    done
    test -s $out/de.bmap
  '';

  # pd as a single file, for live ISOs: `python3 pd.pyz open ...`.
  pdPyz = pkgs.runCommand "pd.pyz" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    mkdir src
    cp -r --no-preserve=mode ${src}/pd src/
    rm -rf src/pd/__pycache__
    python3 -m zipapp src -o $out -p "/usr/bin/env python3" -m "pd.cli:entry"
  '';

  # QEMU with CanoKey, an emulated FIDO2 security key (tests only), patched
  # so that two emulated keys are actually different:
  # * its random numbers come from an LCG seeded with 0, so every new key got
  #   the same secrets; use getrandom();
  # * it keeps littlefs's caches when a key is hot-plugged with a different
  #   state file (the tests swap keys to enroll several); remount instead.
  canokeyQemu = pkgs.canokey-qemu.overrideAttrs (old: {
    postPatch = (old.postPatch or "") + ''
      substituteInPlace canokey-core/canokey-crypto/src/rand.c \
        --replace-fail '#include <rand.h>' '#include <rand.h>
      #include <sys/random.h>' \
        --replace-fail '__attribute__((weak)) uint32_t random32(void) {' \
          '__attribute__((weak)) uint32_t random32(void) { uint32_t r; if (getrandom(&r, sizeof r, 0) == sizeof r) return r;' \
        --replace-fail '__attribute__((weak)) void random_buffer(uint8_t *buf, size_t len) {' \
          '__attribute__((weak)) void random_buffer(uint8_t *buf, size_t len) { if (getrandom(buf, len, 0) == (ssize_t)len) return;'
      substituteInPlace canokey-core/src/fs.c --replace-fail \
        'int fs_mount(const struct lfs_config *cfg) { return lfs_mount(&lfs, cfg); }' \
        'static int fs_mounted; int fs_mount(const struct lfs_config *cfg) { if (fs_mounted) lfs_unmount(&lfs); int err = lfs_mount(&lfs, cfg); fs_mounted = !err; return err; }'
    '';
  });
  qemuCanokey = pkgs.qemu_kvm.override { canokeySupport = true; canokey-qemu = canokeyQemu; };

  toolvm = import ./toolvm.nix { inherit pkgs kernel plainTree bootTools fido2Hmac; modulesClosure = toolModulesClosure; };

  setupEnv = import ./setup-env.nix { inherit pkgs kernel plainTree src fido2Hmac; modulesClosure = setupModulesClosure; };

  faketarget = import ./faketarget.nix { inherit pkgs modulesClosure bootTools plainTools src; };

  # Everything the test harness needs, at fixed names.
  testenv = pkgs.linkFarm "pd-testenv" [
    { name = "bzImage"; path = "${kernel}/bzImage"; }
    { name = "boot-initrd.cpio.zst"; path = bootInitrd; }
    { name = "toolvm-initrd.cpio.zst"; path = toolvm.initrd; }
    { name = "toolenv"; path = toolvm.toolEnv; }
    { name = "plain"; path = plainTree; }
    { name = "boot-tools"; path = bootTools; }
    { name = "stub.efi"; path = "${pkgs.systemd}/lib/systemd/boot/efi/linuxx64.efi.stub"; }
    { name = "ukify"; path = pkgs.systemdUkify; }
    { name = "ovmf"; path = pkgs.OVMF.fd; }
    { name = "busybox"; path = pkgs.pkgsStatic.busybox; }
    { name = "faketarget-initrd.cpio.zst"; path = faketarget; }
    { name = "setup-initrd.cpio.zst"; path = setupEnv.initrd; }
    { name = "keymaps"; path = keymaps; }
    # QEMU with an emulated FIDO2 security key (CanoKey), for the FIDO2 tests.
    { name = "qemu-canokey"; path = qemuCanokey; }
    # Extracts a live ISO's kernel and initrd (test_live).
    { name = "xorriso"; path = pkgs.xorriso; }
  ];
in
{
  inherit testenv toolvm setupEnv pdPyz;
  inherit bootTools plainTools bootInitrd plainTree modulesClosure detectShift fido2Hmac lvm2Static;
  kernelImage = "${kernel}/bzImage";
  kernel = kernel;
  ukify = pkgs.systemdUkify;
  stub = "${pkgs.systemd}/lib/systemd/boot/efi/linuxx64.efi.stub";
  ovmf = pkgs.OVMF.fd;
}
