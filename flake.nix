{
  description = "Throttler development environment";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs = { nixpkgs, ... }:
    let
      linuxSystems = [ "x86_64-linux" "aarch64-linux" ];
    in
    {
      devShells = nixpkgs.lib.genAttrs linuxSystems (system:
        let
          pkgs = builtins.getAttr system nixpkgs.legacyPackages;
        in
        {
          default = pkgs.mkShellNoCC {
            name = "throttler-dev";
            packages = [ pkgs.python314 pkgs.uv ];

            UV_PYTHON = pkgs.python314.interpreter;
            UV_PYTHON_DOWNLOADS = "never";

            # PyPI's DuckDB wheel needs the C++ runtime on NixOS.
            LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];
          };
        });
    };
}
