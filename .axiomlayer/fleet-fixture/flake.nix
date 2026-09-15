{
  description = "Public, read-only AxiomLayer nix-homebrew integration fixture";

  inputs = {
    nixpkgs.url = "github:axiomlayer/nixpkgs/c3eea5b2156db11c7eeeada3dc737711255b253e";

    nix-darwin = {
      url = "github:axiomlayer/nix-darwin/c3e90c89649b07d1a96e4b9dd6cd0d6e44b91a74";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    homebrew-brew = {
      url = "github:axiomlayer/homebrew/08e85c4e42f5d8f1ea17c36cb59cf61c2ccb26c3";
      flake = false;
    };

    nix-homebrew = {
      url = "github:axiomlayer/nix-homebrew/09a921d0181146cf6163ec2cc1db7b6fd539a885";
      inputs.brew-src.follows = "homebrew-brew";
    };
  };

  outputs =
    inputs@{
      homebrew-brew,
      nix-darwin,
      nix-homebrew,
      nixpkgs,
      ...
    }:
    let
      mkFleet = system:
        nix-darwin.lib.darwinSystem {
          inherit system;
          specialArgs = { inherit inputs; };
          modules = [
            nix-homebrew.darwinModules.nix-homebrew
            ./fleet-darwin.nix
          ];
        };

      configurations = {
        fleet-arm64 = mkFleet "aarch64-darwin";
        fleet-intel = mkFleet "x86_64-darwin";
      };

      configurationFor = system:
        if system == "aarch64-darwin" then configurations.fleet-arm64 else configurations.fleet-intel;

      contractFor = system:
        let
          cfg = (configurationFor system).config;
          brewPath = toString homebrew-brew;
        in
        {
          inherit system;
          activation = "not-executed";
          autoMigrate = cfg.nix-homebrew.autoMigrate;
          brewEntrypointPresent = builtins.pathExists "${brewPath}/bin/brew";
          brewLibraryPresent = builtins.pathExists "${brewPath}/Library/Homebrew";
          brewSourceFollows = toString cfg.nix-homebrew.package == brewPath;
          enableRosetta = cfg.nix-homebrew.enableRosetta;
          intelPrefixEnabled = cfg.nix-homebrew.prefixes."/usr/local".enable;
          armPrefixEnabled = cfg.nix-homebrew.prefixes."/opt/homebrew".enable;
          mutableTaps = cfg.nix-homebrew.mutableTaps;
          nixManagedByDarwin = cfg.nix.enable;
          primaryUser = cfg.system.primaryUser;
          nixHomebrewSource = toString nix-homebrew;
          stateVersion = cfg.system.stateVersion;
          toplevelDerivation = cfg.system.build.toplevel.drvPath;
        };

      writeContract = system:
        nixpkgs.legacyPackages.${system}.writeText
          "axiomlayer-nix-homebrew-${system}.json"
          (builtins.toJSON (contractFor system));
    in
    {
      darwinConfigurations = configurations;

      fleetContracts = {
        aarch64-darwin = contractFor "aarch64-darwin";
        x86_64-darwin = contractFor "x86_64-darwin";
      };

      checks = {
        aarch64-darwin.fleet-contract = writeContract "aarch64-darwin";
        x86_64-darwin.fleet-contract = writeContract "x86_64-darwin";
      };
    };
}
