{ patchelf }:
# patchelf 0.19.2 corrupts executables whose headers it must move: running
# --set-interpreter before --set-rpath (autoPatchelf's order) leaves a
# read-only LOAD overlapping the RW one, so ld.so faults on .dynamic and
# node segfaults on start (NixOS/patchelf#674). Fixed upstream by #672, not
# yet released; drop this once nixpkgs ships a patchelf that contains it.
# Vendored src/* diff from NixOS/patchelf commit
# 59acc3d98b4961cfddf02090d9426ac735603218 (the former fetchpatch includes).
# Builds must not depend on github.com's patch endpoint being available.
patchelf.overrideAttrs (old: {
  patches = (old.patches or [ ]) ++ [
    ./patches/patchelf-fix-executable-header-load-overlap.patch
  ];
})
