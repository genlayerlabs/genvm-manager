# genvm-tool: the multi-repo `configure` / `git ls` helpers and the
# `genvm-tool test` runner (under `genvm_tool/tests`, shipped with the package).
{
  pkgs ? import <nixpkgs> { },
  python ? pkgs.python312,
}:

python.pkgs.buildPythonApplication {
  pname = "genvm-tool";
  version = "0.1.0";
  src = pkgs.lib.cleanSourceWith {
    src = ./.;
    filter =
      path: type:
      pkgs.lib.cleanSourceFilter path type
      && !pkgs.lib.hasSuffix ".nix" path
      && baseNameOf path != "flake.lock";
  };

  pyproject = true;
  build-system = [ python.pkgs.setuptools ];
  dependencies = with python.pkgs; [
    aiohttp
    jsonnet
    shtab
    argparse-manpage
    ruamel-yaml
    cloudpickle
    python-dotenv
    questionary
  ];

  nativeBuildInputs = [
    pkgs.git
    pkgs.installShellFiles
    pkgs.pre-commit
  ];

  doCheck = false;
  dontUsePytestCheck = true;

  # Generate the shell completions and man page from the freshly built binary
  # (shtab / argparse-manpage both walk the whole argparse command tree) and drop
  # them in the standard share/ locations.
  postInstall = ''
    installShellCompletion --cmd genvm-tool \
      --bash <($out/bin/genvm-tool --print-completion bash) \
      --zsh <($out/bin/genvm-tool --print-completion zsh)

    $out/bin/genvm-tool --print-manpage > genvm-tool.1
    installManPage genvm-tool.1
  '';

  meta = {
    description = "GenVM monorepo git helper (build, tests, hooks, ls)";
    mainProgram = "genvm-tool";
  };
}
