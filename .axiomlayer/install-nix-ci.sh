#!/bin/sh
set -eu
umask 077

SYSTEM_PATH=/usr/bin:/bin:/usr/sbin:/sbin
PATH=$SYSTEM_PATH
export PATH

# Disposable hosted-CI bootstrap only. Fleet devices consume the promoted
# AxiomLayer Nix foundation; this wrapper verifies the official release
# launcher before executing it with the credential environment removed.
NIX_VERSION=2.35.2
INSTALLER_URL=https://releases.nixos.org/nix/nix-2.35.2/install
INSTALLER_SHA256=9adda97297d9e8ab360df95c729eabff4f4f93d6db091953c3a68f29e3fb130c

case "$(uname -s).$(uname -m)" in
  Darwin.arm64|Darwin.aarch64)
    PLATFORM_SHA256=1695c13aba5afa7c2ecd6dc4a9393f602e7bbc440ed45e81602c831546580ec3
    ;;
  Darwin.x86_64)
    PLATFORM_SHA256=d725518d89f3b0b8d4af702a9d38d519814014cbe125afb3ed0545c9d755f6a5
    ;;
  Linux.aarch64)
    PLATFORM_SHA256=4d0302a2910f5eec1c33b8deef634f04899a75737e7001ec49908d003ae5efda
    ;;
  Linux.x86_64)
    PLATFORM_SHA256=0c3960a9792331a22081c3c7a5d8465db9b17c50b3acdf18587fa4c6f2cb1158
    ;;
  *)
    printf '%s\n' "unsupported Nix CI host: $(uname -s).$(uname -m)" >&2
    exit 1
    ;;
esac

temporary_parent_input=${TMPDIR:-/tmp}
case "$temporary_parent_input" in
  /*) ;;
  *)
    printf '%s\n' 'temporary directory parent must be absolute' >&2
    exit 1
    ;;
esac
if [ ! -d "$temporary_parent_input" ] || [ -L "$temporary_parent_input" ]; then
  printf '%s\n' 'temporary directory parent must be a regular directory' >&2
  exit 1
fi
temporary_parent=$(CDPATH= cd -P -- "$temporary_parent_input" && pwd -P)
if [ "$temporary_parent" = / ]; then
  printf '%s\n' 'refusing the filesystem root as a temporary directory parent' >&2
  exit 1
fi

temporary_directory=$(mktemp -d "$temporary_parent/axiom-nix-ci.XXXXXXXX")
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM
if [ ! -d "$temporary_directory" ] || [ -L "$temporary_directory" ]; then
  printf '%s\n' 'temporary directory must be a real directory' >&2
  exit 1
fi
temporary_directory=$(CDPATH= cd -P -- "$temporary_directory" && pwd -P)
temporary_observed_parent=${temporary_directory%/*}
temporary_name=${temporary_directory##*/}
if [ "$temporary_observed_parent" != "$temporary_parent" ]; then
  printf '%s\n' 'temporary directory escaped the reviewed physical parent' >&2
  exit 1
fi
case "$temporary_name" in
  axiom-nix-ci.????????) ;;
  *)
    printf '%s\n' 'temporary directory has an unexpected generated name' >&2
    exit 1
    ;;
esac

installer=$temporary_directory/install

curl --fail --location --proto '=https' --tlsv1.2 \
  --silent --show-error --output "$installer" "$INSTALLER_URL"

actual_installer_sha=$(shasum -a 256 "$installer" | awk '{print $1}')
if [ "$actual_installer_sha" != "$INSTALLER_SHA256" ]; then
  printf '%s\n' "Nix installer digest drift: $actual_installer_sha" >&2
  exit 1
fi
if ! grep -F "hash=$PLATFORM_SHA256" "$installer" >/dev/null; then
  printf '%s\n' "Nix $NIX_VERSION installer lacks expected platform digest" >&2
  exit 1
fi

clean_home=$temporary_directory/home
mkdir -p "$clean_home"
ci_user=$(id -un)
# The reviewed installer receives no GitHub token, Actions runtime token,
# credential helper, proxy credential, or repository-controlled environment.
env -i \
  HOME="$clean_home" \
  USER="$ci_user" \
  LOGNAME="$ci_user" \
  PATH="$SYSTEM_PATH" \
  TMPDIR="$temporary_directory" \
  CI=true \
  sh "$installer" --daemon --yes --no-channel-add --no-modify-profile

profile=/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
if [ ! -r "$profile" ]; then
  printf '%s\n' 'Nix daemon profile was not installed' >&2
  exit 1
fi
# shellcheck disable=SC1091
. "$profile"
if [ "$(nix --version)" != "nix (Nix) $NIX_VERSION" ]; then
  printf '%s\n' "installed Nix version drift: $(nix --version)" >&2
  exit 1
fi
if [ -n "${GITHUB_PATH:-}" ]; then
  printf '%s\n' /nix/var/nix/profiles/default/bin >> "$GITHUB_PATH"
fi
printf '%s\n' "nix=verified version=$NIX_VERSION installer=$INSTALLER_SHA256 platform=$PLATFORM_SHA256"
