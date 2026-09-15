{
  config,
  inputs,
  lib,
  pkgs,
  ...
}:

let
  brewPath = toString inputs.homebrew-brew;
in
{
  assertions = [
    {
      assertion = toString config.nix-homebrew.package == brewPath;
      message = "nix-homebrew brew-src must follow the promoted homebrew-brew input";
    }
    {
      assertion = builtins.pathExists "${brewPath}/bin/brew";
      message = "AxiomLayer promoted Homebrew source must contain bin/brew";
    }
    {
      assertion = builtins.pathExists "${brewPath}/Library/Homebrew";
      message = "AxiomLayer promoted Homebrew source must contain Library/Homebrew";
    }
  ];

  # The runtime foundation owns Nix itself. This fixture only evaluates the
  # declarative consumer boundary and never activates a host.
  nix.enable = false;

  system = {
    primaryUser = "fleet-ci";
    stateVersion = 7;
  };

  users.users.fleet-ci.home = "/Users/fleet-ci";

  nix-homebrew = {
    enable = true;
    enableRosetta = pkgs.stdenv.hostPlatform.isAarch64;
    user = "fleet-ci";
    autoMigrate = false;
    mutableTaps = false;
    patchBrew = true;
    taps = { };
    extraEnv.HOMEBREW_NO_ANALYTICS = "1";
    enableBashIntegration = false;
    enableFishIntegration = false;
    enableZshIntegration = false;
  };

  homebrew = {
    enable = false;
    taps = [ ];
    brews = [ ];
    casks = [ ];
    masApps = { };
  };

  networking = {
    hostName = lib.mkDefault "fleet-nix-homebrew-ci";
    localHostName = lib.mkDefault "fleet-nix-homebrew-ci";
    computerName = lib.mkDefault "AxiomLayer nix-homebrew CI";
  };
}
