{
  description = "genvm-tool unit-test Python environment (pinned, independent flake)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
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
        python = pkgs.python312;
        # The tool itself, only to borrow its dependency list: duplicating the
        # one in `default.nix` here is what would drift.
        tool = pkgs.callPackage ./default.nix { inherit python; };
        # Instruments a fuzz target for AFL; not in nixpkgs. The same derivation
        # is in executors/v0.3.x/support/nix/py-test/flake.nix, which cannot be
        # shared from here: it lives in another repository.
        pythonAfl = python.pkgs.buildPythonPackage rec {
          pname = "python-afl";
          version = "0.7.3";
          pyproject = true;

          src = pkgs.fetchPypi {
            inherit pname version;
            hash = "sha256-s3MZbXRyZiMLvoQu2qrWWWJTY4V8jnZVBddl0GE/lUQ=";
          };

          build-system = with python.pkgs; [
            cython
            setuptools
            wheel
          ];
          pythonImportsCheck = [ "afl" ];
        };
        # `genvm_tool` is NOT installed into this env. The pytest plugin puts the
        # project root on PYTHONPATH so the tests import the working tree, the
        # same way genlayer-py-std's tests import `src/`.
        pythonEnv = python.withPackages (
          ps:
          [ ps.pytest ]
          ++ tool.propagatedBuildInputs
          ++ pkgs.lib.optionals (system == "x86_64-linux") [ pythonAfl ]
        );
      in
      {
        # AFL++ carries `afl-fuzz` and friends, which the fuzz cases exec from
        # this env's bin/ (the python-afl wrappers are not used)
        packages.default = pkgs.buildEnv {
          name = "genvm-tool-test";
          paths = [
            pythonEnv
          ]
          ++ pkgs.lib.optionals (system == "x86_64-linux") [ pkgs.aflplusplus ];
        };
      }
    );
}
