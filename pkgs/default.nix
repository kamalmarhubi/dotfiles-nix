# Custom packages overlay module
{ lib }:
{
  nixpkgs.overlays = [
    (final: prev: 
      let
        # List of package directories to include
        packageNames = [
        ];

        # Generate packages from the list
        packages = lib.genAttrs packageNames (name: final.callPackage ./${name} {});
      in
      {
        mine = packages;
      }
    )
  ];
}
