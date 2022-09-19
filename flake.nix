{
  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem
      (system: let
        pkgs = import nixpkgs {
          inherit system;
          config.allowUnfreePredicate = pkg: builtins.elem (nixpkgs.lib.getName pkg) [
            "ngrok"
          ];
        };
        pip-tools = pkgs.python310Packages.pip-tools.overrideAttrs (old: {
          doInstallCheck = false;
        });
      in {
        devShell = pkgs.mkShell {
          buildInputs = with pkgs; [
            ngrok
            pip-tools
          ];
        };
      });
}
