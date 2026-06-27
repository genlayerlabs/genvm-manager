{
  description = "genvm-manager pre-commit lint tools (pinned, independent of the umbrella flake)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        # One named output per tool: hooks.toml `nix = "<name>"` references
        # these, and they are what `default` aggregates.
        tools = {
          inherit (pkgs)
            ruff
            stylua
            prettier
            editorconfig-checker
            check-jsonschema
            rustfmt
            nixfmt
            cargo
            ;
          pre-commit-hooks = pkgs.python3Packages.pre-commit-hooks;
        };
      in
      {
        packages = tools // {
          default = pkgs.buildEnv {
            name = "genvm-precommit-tools-manager";
            # pre-commit-hooks ships a `python`/scripts that can collide.
            ignoreCollisions = true;
            paths = builtins.attrValues tools;
          };
        };
      }
    );
}
