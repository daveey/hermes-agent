# Keep the locked source bytes when upstream download endpoints disappear.
{ inputs, ... }:
{
  perSystem =
    { system, pkgs, ... }:
    {
      # Exercise the real fetcher in flake check, even without a desktop build.
      checks.libtirpc-source = pkgs.libtirpc.src;

      _module.args.pkgs = import inputs.nixpkgs {
        inherit system;
        overlays = [
          (final: prev: {
            # linux-nfs.org migrated from gitweb to cgit: the old snapshot
            # query now returns HTML. This content-addressed NixOS mirror
            # serves the identical archive, verified against the original hash.
            libtirpc = prev.libtirpc.overrideAttrs (
              old:
              final.lib.optionalAttrs (old.version == "1.3.7") {
                src = old.src.overrideAttrs (_: {
                  urls = [
                    "https://tarballs.nixos.org/sha256/5467ed12bdf1cc2a7c3b7a2a0a3219a3396ae7df60c4de48b07051686dfb18fe"
                  ];
                });
              }
            );
          })
        ];
      };
    };
}
