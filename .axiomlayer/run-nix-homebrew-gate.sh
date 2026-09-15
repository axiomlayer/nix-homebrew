#!/usr/bin/env bash

set -euo pipefail
umask 077

usage() {
  echo "usage: run-nix-homebrew-gate.sh evaluate|native candidate|upstream-main SYSTEM" >&2
  exit 2
}

mode=${1:-}
candidate_kind=${2:-}
system=${3:-}

case "$mode" in
  evaluate | native) ;;
  *) usage ;;
esac
case "$candidate_kind" in
  candidate | upstream-main) ;;
  *) usage ;;
esac
case "$system" in
  aarch64-darwin | x86_64-darwin) ;;
  *) usage ;;
esac

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
fixture="$root/.axiomlayer/fleet-fixture"
scratch_parent_input=${RUNNER_TEMP:-${TMPDIR:-/tmp}}
case "$scratch_parent_input" in
  /*) ;;
  *)
    echo "temporary directory parent must be absolute" >&2
    exit 1
    ;;
esac
if [ ! -d "$scratch_parent_input" ] || [ -L "$scratch_parent_input" ]; then
  echo "temporary directory parent must be a regular directory" >&2
  exit 1
fi
scratch_parent=$(CDPATH= cd -P -- "$scratch_parent_input" && pwd -P)
if [ "$scratch_parent" = / ]; then
  echo "refusing the filesystem root as a temporary directory parent" >&2
  exit 1
fi

scratch=$(mktemp -d "$scratch_parent/axiom-nix-homebrew-gate.XXXXXX")
trap 'rm -rf "$scratch"' EXIT
if [ ! -d "$scratch" ] || [ -L "$scratch" ]; then
  echo "temporary directory must be a real directory" >&2
  exit 1
fi
scratch=$(CDPATH= cd -P -- "$scratch" && pwd -P)
scratch_observed_parent=${scratch%/*}
scratch_name=${scratch##*/}
if [ "$scratch_observed_parent" != "$scratch_parent" ]; then
  echo "temporary directory escaped the reviewed physical parent" >&2
  exit 1
fi
case "$scratch_name" in
  axiom-nix-homebrew-gate.??????) ;;
  *)
    echo "temporary directory has an unexpected generated name" >&2
    exit 1
    ;;
esac

locked="$scratch/locked"
candidate="$scratch/candidate"
clean_home="$scratch/home"
mkdir -p "$clean_home"

ci_user=$(id -un)
clean_python() {
  env -i \
    HOME="$clean_home" \
    USER="$ci_user" \
    LOGNAME="$ci_user" \
    PATH="$PATH" \
    TMPDIR="$scratch" \
    CI=true \
    python3 -I -B "$@"
}

clean_python "$root/.axiomlayer/check.py" materialize \
  --revision locked \
  --destination "$locked"
clean_python "$root/.axiomlayer/check.py" materialize \
  --revision "$candidate_kind" \
  --destination "$candidate"

observed_nix=$(nix --version)
test "$observed_nix" = "nix (Nix) 2.35.2"

promoted_revision=$(
  clean_python "$root/.axiomlayer/check.py" promoted-homebrew-revision
)

nix_command() {
  env -i \
    HOME="$clean_home" \
    USER="$ci_user" \
    LOGNAME="$ci_user" \
    PATH="$PATH" \
    TMPDIR="$scratch" \
    CI=true \
    nix \
    --extra-experimental-features "nix-command flakes" \
    --option accept-flake-config false \
    --option allow-import-from-derivation false \
    --option flake-registry "" \
    --option sandbox true \
    --option sandbox-fallback false \
    "$@"
}

configuration_for() {
  case "$system" in
    aarch64-darwin) echo fleet-arm64 ;;
    x86_64-darwin) echo fleet-intel ;;
  esac
}

validate_contract() {
  payload_kind=$1
  payload=$2
  source_name=$3
  python3 -I -B - "$payload_kind" "$payload" "$system" "$source_name" <<'PY'
import json
import pathlib
import re
import sys

payload_kind, payload, system, source_name = sys.argv[1:]
def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SystemExit(f"{source_name}/{system}: duplicate contract key: {key}")
        result[key] = value
    return result

if payload_kind == "file":
    contract = json.loads(
        pathlib.Path(payload).read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicates,
    )
else:
    contract = json.loads(payload, object_pairs_hook=reject_duplicates)

is_arm = system == "aarch64-darwin"
expected = {
    "activation": "not-executed",
    "armPrefixEnabled": is_arm,
    "autoMigrate": False,
    "brewEntrypointPresent": True,
    "brewLibraryPresent": True,
    "brewSourceFollows": True,
    "enableRosetta": is_arm,
    "intelPrefixEnabled": True,
    "mutableTaps": False,
    "nixManagedByDarwin": False,
    "primaryUser": "fleet-ci",
    "stateVersion": 7,
    "system": system,
}
if set(contract) != set(expected) | {"nixHomebrewSource", "toplevelDerivation"}:
    raise SystemExit(f"{source_name}/{system}: contract key inventory changed")
for key, value in expected.items():
    if contract.get(key) != value:
        raise SystemExit(
            f"{source_name}/{system}: contract mismatch for {key}: "
            f"{contract.get(key)!r} != {value!r}"
        )
if not re.fullmatch(r"/nix/store/[a-z0-9]+-.*\.drv", contract.get("toplevelDerivation", "")):
    raise SystemExit(f"{source_name}/{system}: missing Darwin toplevel derivation")
if not contract.get("nixHomebrewSource", "").startswith("/nix/store/"):
    raise SystemExit(f"{source_name}/{system}: nix-homebrew source was not materialized")
PY
}

evaluate_source() {
  source_name=$1
  source_path=$2
  configuration=$(configuration_for)

  # Materialize every inherited lock without evaluating an activation script.
  nix_command flake metadata --no-write-lock-file "$source_path" >/dev/null
  nix_command flake metadata --no-write-lock-file "$source_path/ci" >/dev/null

  drv_path=$(
    nix_command eval \
      --raw \
      --no-write-lock-file \
      "$fixture#darwinConfigurations.$configuration.config.system.build.toplevel.drvPath" \
      --override-input nix-homebrew "path:$source_path"
  )
  case "$drv_path" in
    /nix/store/*.drv) ;;
    *)
      echo "$source_name/$system returned an invalid Darwin derivation: $drv_path" >&2
      exit 1
      ;;
  esac

  contract=$(
    nix_command eval \
      --json \
      --no-write-lock-file \
      "$fixture#fleetContracts.$system" \
      --override-input nix-homebrew "path:$source_path"
  )
  validate_contract json "$contract" "$source_name"
  echo "darwin_evaluation=verified source=$source_name system=$system"
}

prove_promoted_refusal() {
  source_name=$1
  source_path=$2
  configuration=$(configuration_for)
  stdout="$scratch/$source_name-$system-promoted.stdout"
  stderr="$scratch/$source_name-$system-promoted.stderr"

  if nix_command eval \
    --raw \
    --no-write-lock-file \
    "$fixture#darwinConfigurations.$configuration.config.system.build.toplevel.drvPath" \
    --override-input nix-homebrew "path:$source_path" \
    --override-input homebrew-brew "github:axiomlayer/homebrew/$promoted_revision" \
    >"$stdout" 2>"$stderr"
  then
    echo "promoted Homebrew candidate unexpectedly evaluated for $source_name/$system" >&2
    exit 1
  fi

  expected="AxiomLayer promoted Homebrew source must contain Library/Homebrew"
  if ! grep -F "$expected" "$stderr" >/dev/null; then
    echo "promoted Homebrew candidate failed for an unreviewed reason" >&2
    sed -n '1,160p' "$stderr" >&2
    exit 1
  fi
  echo "promoted_brew=quarantined source=$source_name system=$system revision=$promoted_revision"
}

build_native_contract() {
  source_name=$1
  source_path=$2
  observed_system=$(nix_command eval --impure --raw --expr builtins.currentSystem)
  if [ "$observed_system" != "$system" ]; then
    echo "hosted runner architecture mismatch: expected $system, got $observed_system" >&2
    exit 1
  fi

  contract_path=$(
    nix_command build \
      --no-link \
      --print-out-paths \
      --no-write-lock-file \
      "$fixture#checks.$system.fleet-contract" \
      --override-input nix-homebrew "path:$source_path"
  )
  test -f "$contract_path"
  validate_contract file "$contract_path" "$source_name"
  echo "darwin_native_contract=verified source=$source_name system=$system"
}

evaluate_source locked "$locked"
evaluate_source candidate "$candidate"
prove_promoted_refusal locked "$locked"
prove_promoted_refusal candidate "$candidate"

if [ "$mode" = native ]; then
  build_native_contract locked "$locked"
  build_native_contract candidate "$candidate"
fi

echo "nix_homebrew_gate=verified mode=$mode candidate_kind=$candidate_kind system=$system"
