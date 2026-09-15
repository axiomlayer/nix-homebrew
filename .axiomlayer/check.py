#!/usr/bin/env python3
"""Fail-closed checks for the AxiomLayer nix-homebrew integration fork."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / ".axiomlayer"
FIXTURE = INTEGRATION / "fleet-fixture"
PINS_FILE = INTEGRATION / "pins.json"
ACTIVE_WORKFLOW = Path(".github/workflows/axiomlayer-integration.yml")
ARCHIVE_DIRECTORY = Path(".github/upstream-workflows")
WORKFLOW_ARCHIVE_BASELINE = "09a921d0181146cf6163ec2cc1db7b6fd539a885"
WORKFLOW_ARCHIVE_SHA256 = (
    "a70aabe038a1e85133d6168ba774a8d5af30ff6a4ea67f786f72f8eca89cc5f6"
)
WORKFLOW_ARCHIVE_BLOB = "5664d11c181e939e6a5dc766c6d1dae8a03d819c"
WORKFLOW_ARCHIVE_MODE = "100644"
EXPECTED_WORKFLOWS = {
    ACTIVE_WORKFLOW,
}
EXPECTED_WORKFLOW_ISOLATION = {
    "active": [str(ACTIVE_WORKFLOW)],
    "archiveBaseline": WORKFLOW_ARCHIVE_BASELINE,
    "archives": [
        {
            "sourcePath": ".github/workflows/ci.yaml",
            "archivePath": ".github/upstream-workflows/ci.yaml.disabled",
            "blobSha1": WORKFLOW_ARCHIVE_BLOB,
            "mode": WORKFLOW_ARCHIVE_MODE,
            "sha256": WORKFLOW_ARCHIVE_SHA256,
        }
    ],
}
INTEGRATION_JOBS = (
    "contract",
    "darwin-evaluation",
    "darwin-native-contract",
    "integration-gate",
)
CANONICAL_REPOSITORY = "axiomlayer/nix-homebrew"
CANONICAL_BRANCH = "main"
CANONICAL_REF = "refs/heads/main"
CANONICAL_WORKFLOW_REF = (
    "axiomlayer/nix-homebrew/.github/workflows/"
    "axiomlayer-integration.yml@refs/heads/main"
)
CANONICAL_PR_WORKFLOW_PREFIX = (
    "axiomlayer/nix-homebrew/.github/workflows/axiomlayer-integration.yml@refs/pull/"
)
PROACTIVE_EVENTS = ("schedule", "workflow_dispatch")
INTEGRATION_CONTEXT_GUARD = (
    "github.repository == 'axiomlayer/nix-homebrew' && "
    "github.event.repository.default_branch == 'main' && "
    "(((github.event_name == 'schedule' || github.event_name == 'workflow_dispatch') && "
    "github.ref == 'refs/heads/main' && "
    "github.ref_protected == true && "
    "github.workflow_ref == 'axiomlayer/nix-homebrew/.github/workflows/"
    "axiomlayer-integration.yml@refs/heads/main') || "
    "(github.event_name == 'push' && github.ref == 'refs/heads/main' && "
    "github.ref_protected == true && "
    "github.workflow_ref == 'axiomlayer/nix-homebrew/.github/workflows/"
    "axiomlayer-integration.yml@refs/heads/main') || "
    "(github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name == github.repository && "
    "github.base_ref == 'main' && "
    "github.ref == format('refs/pull/{0}/merge', github.event.pull_request.number) && "
    "github.sha == github.event.pull_request.merge_commit_sha && "
    "github.workflow_ref == format('axiomlayer/nix-homebrew/.github/workflows/"
    "axiomlayer-integration.yml@refs/pull/{0}/merge', "
    "github.event.pull_request.number)))"
)
EXPECTED_INTEGRATION_TRIGGER = """on:
  pull_request:
    branches:
      - main
  push:
    branches:
      - main
  schedule:
    - cron: "29 7 * * *"
  workflow_dispatch:"""
EXPECTED_TERMINAL_JOB_BODY = """    if: always()
    needs:
      - contract
      - darwin-evaluation
      - darwin-native-contract
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - name: Require every applicable read-only proof
        env:
          CONTRACT_RESULT: ${{ needs.contract.result }}
          EVALUATION_RESULT: ${{ needs.darwin-evaluation.result }}
          NATIVE_RESULT: ${{ needs.darwin-native-contract.result }}
        run: |
          test "$CONTRACT_RESULT" = success
          test "$EVALUATION_RESULT" = success
          test "$NATIVE_RESULT" = success"""
EXPECTED_RUNS_ON = (
    "ubuntu-24.04",
    "ubuntu-24.04",
    "${{ matrix.runner }}",
    "ubuntu-24.04",
)
EXPECTED_MATRIX_RUNNERS = ("macos-15", "macos-15-intel")
EXPECTED_ARCHIVED_ACTION_REFS = (
    "actions/checkout@v7.0.1",
    "samueldr/lix-gha-installer-action@v2025-10-27",
    "actions/checkout@v7.0.1",
    "samueldr/lix-gha-installer-action@v2025-10-27",
)
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
NAR_HASH = re.compile(r"^sha256-[A-Za-z0-9+/]{43}=$")
ACTION_REFERENCE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)


class ContractError(RuntimeError):
    """Raised when a fork integration invariant is not satisfied."""


def refuse(message: str) -> None:
    raise ContractError(message)


def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict:
    parsed: dict = {}
    for key, value in pairs:
        if key in parsed:
            refuse(f"JSON contains duplicate key {key!r}")
        parsed[key] = value
    return parsed


def reject_json_constant(value: str) -> None:
    refuse(f"JSON contains non-standard constant {value!r}")


def read_regular_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink():
        refuse(f"{label} must not be a symlink")
    if not path.is_file():
        refuse(f"{label} must be a regular file")
    return path.read_bytes()


def read_regular_text(path: Path, label: str) -> str:
    return read_regular_bytes(path, label).decode("utf-8")


def parse_json_bytes(raw: bytes, label: str) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_json_constant,
        )
    except UnicodeDecodeError as error:
        refuse(f"{label} is not valid UTF-8: {error}")


def load_json_file(path: Path, label: str) -> object:
    return parse_json_bytes(read_regular_bytes(path, label), label)


def load_pins() -> dict:
    pins = load_json_file(PINS_FILE, "integration pin manifest")
    if not isinstance(pins, dict):
        refuse("integration pin manifest must be a JSON object")
    return pins


def run(*args: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        capture_output=capture,
        text=True,
    )


def verify_pins(pins: dict) -> None:
    exact = {
        "schema": "axiomlayer-nix-homebrew-integration-v1",
        "upstream": "zhaofengli/nix-homebrew",
        "fork": "axiomlayer/nix-homebrew",
        "sourceBranch": "main",
        "sourceCommit": "09a921d0181146cf6163ec2cc1db7b6fd539a885",
        "sourceTree": "82039b915c174b9199e93561a7f1eca6754f8daa",
    }
    for key, expected in exact.items():
        if pins.get(key) != expected:
            refuse(f"{key} must be {expected!r}, got {pins.get(key)!r}")

    if pins.get("workflowIsolation") != EXPECTED_WORKFLOW_ISOLATION:
        refuse("the workflow archive inventory or immutable baseline changed")

    expected_integration_files = {
        ".axiomlayer/fleet-fixture/flake.lock": "5455e8284a9f296fa9e53ba30a044adb3fc659c561029668a00f037d9883c3df",
        ".axiomlayer/fleet-fixture/flake.nix": "c952b7f7028c592aa7bb85daae7f7a43fde53ce80070ab8d196c7831624d632e",
        ".axiomlayer/fleet-fixture/fleet-darwin.nix": "f1a2c6d73e70687d87dde23bde1455904ee79ddcb90e49136d97502e084f2d2c",
        ".axiomlayer/run-nix-homebrew-gate.sh": "f9d1467cd7a156395fbd4abd04a700970fc0d1d8207263c14c6d6e1ba4606dc1",
        ".github/workflows/axiomlayer-integration.yml": "92c6a5d3cef9bb63a492bde5fcdd7495d59627580697e98dd7374a2e5916523e",
    }
    if pins.get("integrationFileSha256") != expected_integration_files:
        refuse("the reviewed integration file digest inventory changed")

    if pins.get("forkEvidence") != {
        "path": ".axiomlayer/evidence/repository.json",
        "sha256": "2cd5bcd66e5e698ac4fda63f5e350afaf68ba0c197dc4d42a2b74f7dbf98e2db",
    }:
        refuse("the true-fork evidence reference changed")

    archive = pins.get("sourceArchive", {})
    expected_url = (
        "https://codeload.github.com/axiomlayer/nix-homebrew/tar.gz/"
        + pins["sourceCommit"]
    )
    if archive.get("url") != expected_url:
        refuse("source archive must resolve through the exact AxiomLayer fork commit")
    if not SHA256.fullmatch(archive.get("sha256", "")):
        refuse("source archive is not pinned by a full SHA-256")
    if not NAR_HASH.fullmatch(archive.get("narHash", "")):
        refuse("source archive is not pinned by a NAR hash")

    policy = pins.get("policySnapshot", {})
    expected_policy = {
        "repository": "axiomlayer/dotfiles",
        "pullRequest": 49,
        "revision": "28df122c5685be02605eb5f2880fc155cd4b21cd",
        "path": "config/upstream-promotion-policy.json",
        "evidencePath": ".axiomlayer/evidence/upstream-promotion-policy.json",
        "sha256": "c782e3305a6e23b631ccd586cde3f790e95aa827756394c96cfc915f39193b89",
        "sourceCount": 18,
        "missingSource": "nix-homebrew",
    }
    if policy != expected_policy:
        refuse("the authoritative 18-source policy snapshot changed")

    root_brew = pins.get("rootLock", {}).get("brew-src", {})
    expected_root_brew = {
        "owner": "Homebrew",
        "repo": "brew",
        "ref": "6.0.22",
        "revision": "08e85c4e42f5d8f1ea17c36cb59cf61c2ccb26c3",
        "narHash": "sha256-NbwVKwKLFl0oXub7oPjvmDaOygCtV2oboeKTu4xXFTk=",
        "lastModified": 1788591592,
        "archiveUrl": "https://codeload.github.com/axiomlayer/homebrew/tar.gz/08e85c4e42f5d8f1ea17c36cb59cf61c2ccb26c3",
        "archiveSha256": "7462e0957a61e91e3bdd48467a8005fd7cd7584baa8a07af83f2a650b3acd3e0",
    }
    if root_brew != expected_root_brew:
        refuse("the upstream nix-homebrew brew-src lock changed")

    expected_ci = {
        "flake-compat": (
            "edolstra",
            "flake-compat",
            "5edf11c44bc78a0d334f6334cdaf7d60d732daab",
            "sha256-vNpUSpF5Nuw8xvDLj2KCwwksIbjua2LZCqhV1LNRDns=",
            1767039857,
        ),
        "nix-darwin_26_05": (
            "nix-darwin",
            "nix-darwin",
            "c3e90c89649b07d1a96e4b9dd6cd0d6e44b91a74",
            "sha256-2cp6N3rrwnGYLTx9l6N+NI+kwrCWxvJUbj5WJhvB29A=",
            1783744694,
        ),
        "nix-darwin_unstable": (
            "nix-darwin",
            "nix-darwin",
            "15abb8c98f336cd8bd840d71059adebabe60bf04",
            "sha256-0tLW8Ff5yt8AH97jw4ZpFJ0OCJ122zIlgWGDmOfU/VU=",
            1785389976,
        ),
        "nix-github-actions": (
            "nix-community",
            "nix-github-actions",
            "f4158fa080ef4503c8f4c820967d946c2af31ec9",
            "sha256-F1G5ifvqTpJq7fdkT34e/Jy9VCyzd5XfJ9TO8fHhJWE=",
            1737420293,
        ),
        "nixpkgs": (
            "NixOS",
            "nixpkgs",
            "705e9929918b43bd7b715dc0a878ac870449bb03",
            "sha256-ViA62qtL5za7V3d5I8OA9q9JcFhsVAiL5jVHwEclWqk=",
            1779622335,
        ),
        "nixpkgs_2": (
            "NixOS",
            "nixpkgs",
            "f205b5574fd0cb7da5b702a2da51507b7f4fdd1b",
            "sha256-/NAkDSsve+GNM0Bt6tleJdCGfsTlK89nPjkVOzZMo0s=",
            1783279667,
        ),
        "nixpkgs_26_05": (
            "NixOS",
            "nixpkgs",
            "70cc4559b10a6062b05ff1af17e0add065ccaed9",
            "sha256-Vux08kA5PICwS2sViCMfwVLAHNoH8TkKAeBo25LjpMI=",
            1786430034,
        ),
        "nixpkgs_unstable": (
            "NixOS",
            "nixpkgs",
            "2fcb964de67fcf60b43471c55d5d99e61a9ccb5a",
            "sha256-RzPPiWeUtuvymnpuEWsdtzli5w4kjZs49FqEs3/1u+I=",
            1786384358,
        ),
    }
    observed_ci = pins.get("ciLocks", {})
    if set(observed_ci) != set(expected_ci):
        refuse("the inherited CI lock inventory changed")
    for name, expected in expected_ci.items():
        node = observed_ci[name]
        observed = (
            node.get("owner"),
            node.get("repo"),
            node.get("revision"),
            node.get("narHash"),
            node.get("lastModified"),
        )
        if observed != expected:
            refuse(f"inherited CI lock {name} changed: {observed!r}")

    expected_integration = {
        "nix-homebrew": (
            "axiomlayer",
            "nix-homebrew",
            pins["sourceCommit"],
            pins["sourceArchive"]["narHash"],
            1788981439,
        ),
        "homebrew-brew": (
            "axiomlayer",
            "homebrew",
            root_brew["revision"],
            root_brew["narHash"],
            root_brew["lastModified"],
        ),
        "nix-darwin": (
            "axiomlayer",
            "nix-darwin",
            "c3e90c89649b07d1a96e4b9dd6cd0d6e44b91a74",
            "sha256-2cp6N3rrwnGYLTx9l6N+NI+kwrCWxvJUbj5WJhvB29A=",
            1783744694,
        ),
        "nixpkgs": (
            "axiomlayer",
            "nixpkgs",
            "c3eea5b2156db11c7eeeada3dc737711255b253e",
            "sha256-vdhpDJ3Lr24lkZ+fDCjmBRRtw9/vcSzkqGJOJyF5h2U=",
            1789344334,
        ),
    }
    observed_integration = pins.get("integrationInputs", {})
    if set(observed_integration) != set(expected_integration):
        refuse("the integration lock inventory changed")
    for name, expected in expected_integration.items():
        node = observed_integration[name]
        observed = (
            node.get("owner"),
            node.get("repo"),
            node.get("revision"),
            node.get("narHash"),
            node.get("lastModified"),
        )
        if observed != expected:
            refuse(f"integration lock {name} changed: {observed!r}")

    promoted = pins.get("promotedHomebrew", {})
    expected_promoted = {
        "upstream": "Homebrew/brew",
        "repository": "axiomlayer/homebrew",
        "revision": "67984c752d13f3bbcb9aba059d727930aec887dc",
        "tree": "e75ef97ec08ccb2c7fcf7916237525141d25614b",
        "policyPath": "config/homebrew-bootstrap.json",
        "policyEvidencePath": ".axiomlayer/evidence/homebrew-bootstrap.json",
        "policySha256": "a7911f476036260326e3f1675e1401519cffc0fcfd466cedbf519e2134e988c6",
        "archiveUrl": "https://codeload.github.com/axiomlayer/homebrew/tar.gz/67984c752d13f3bbcb9aba059d727930aec887dc",
        "archiveSha256": "ce98d15c14c3e243847eed2d7d6e601b8a25529861bca312d448fa76c7b9b801",
        "relationshipToLocked": {
            "status": "diverged",
            "aheadBy": 2,
            "behindBy": 56,
            "mergeBase": "40b08c94d170afa3ec871b29b37fa5b096ffa68a",
        },
        "requiredPaths": ["bin/brew", "Library/Homebrew"],
        "missingPaths": ["Library/Homebrew"],
        "disposition": "blocked-legacy-master-migration-tree",
    }
    if promoted != expected_promoted:
        refuse("the promoted Homebrew quarantine contract changed")

    nix = pins.get("nix", {})
    expected_nix = {
        "version": "2.35.2",
        "sourceRepository": "NixOS/nix",
        "sourceTag": "2.35.2",
        "sourceCommit": "2c73b59da29606068c0c98db015dd3a66955525d",
        "installUrl": "https://releases.nixos.org/nix/nix-2.35.2/install",
        "installerSha256": "9adda97297d9e8ab360df95c729eabff4f4f93d6db091953c3a68f29e3fb130c",
        "wrapperSha256": "d7cba5d4ac1b7a05a6410c61bee18e5c54bd468c363a4749ae42e10443b37143",
        "binaryTarballSha256": {
            "aarch64-darwin": "1695c13aba5afa7c2ecd6dc4a9393f602e7bbc440ed45e81602c831546580ec3",
            "x86_64-darwin": "d725518d89f3b0b8d4af702a9d38d519814014cbe125afb3ed0545c9d755f6a5",
            "aarch64-linux": "4d0302a2910f5eec1c33b8deef634f04899a75737e7001ec49908d003ae5efda",
            "x86_64-linux": "0c3960a9792331a22081c3c7a5d8465db9b17c50b3acdf18587fa4c6f2cb1158",
        },
    }
    if nix != expected_nix:
        refuse("the integration Nix runtime must remain exactly 2.35.2")
    if pins.get("systems") != ["aarch64-darwin", "x86_64-darwin"]:
        refuse("both Darwin architectures must remain in the gate")

    expected_actions = {
        "actions/checkout": {
            "repositoryUrl": "https://github.com/actions/checkout.git",
            "tag": "v7.0.1",
            "revision": "3d3c42e5aac5ba805825da76410c181273ba90b1",
        },
        "samueldr/lix-gha-installer-action": {
            "repositoryUrl": "https://github.com/samueldr/lix-gha-installer-action.git",
            "tag": "v2025-10-27",
            "revision": "8c7f8a4b0f594ab8a6dc3bf71c217587bbc756b5",
        },
    }
    if pins.get("actions") != expected_actions:
        refuse("the reviewed Action commit allowlist changed")
    for action, pin in expected_actions.items():
        if not GIT_SHA.fullmatch(pin["revision"]):
            refuse(f"{action} is not pinned to a full commit SHA")

    expected_acceptance = {
        "margay-activation",
        "margay-homebrew-convergence",
        "margay-gui-application-proof",
        "margay-self-runner-enrollment",
        "margay-cold-boot-resume",
    }
    if set(pins.get("hostAcceptance", [])) != expected_acceptance:
        refuse("Margay host-only acceptance boundaries changed")


def node_tuple(
    node: dict,
) -> tuple[str | None, str | None, str | None, str | None, int | None]:
    locked = node.get("locked", {})
    return (
        locked.get("owner"),
        locked.get("repo"),
        locked.get("rev"),
        locked.get("narHash"),
        locked.get("lastModified"),
    )


def pin_tuple(
    pin: dict,
) -> tuple[str | None, str | None, str | None, str | None, int | None]:
    return (
        pin.get("owner"),
        pin.get("repo"),
        pin.get("revision"),
        pin.get("narHash"),
        pin.get("lastModified"),
    )


def verify_github_lock_node(
    node: dict,
    pin: dict,
    label: str,
    *,
    require_original_revision: bool = False,
    flake: bool = True,
) -> None:
    if set(node) - {"flake", "locked", "original", "inputs"}:
        refuse(f"{label} contains unreviewed top-level fields")
    if flake and "flake" in node:
        refuse(f"{label} unexpectedly changed to a non-flake input")
    if not flake and node.get("flake") is not False:
        refuse(f"{label} changed its flake/non-flake contract")
    locked = node.get("locked")
    original = node.get("original")
    if not isinstance(locked, dict) or not isinstance(original, dict):
        refuse(f"{label} must contain locked and original objects")
    if set(locked) != {"lastModified", "narHash", "owner", "repo", "rev", "type"}:
        refuse(f"{label} locked source schema changed")
    if locked.get("type") != "github" or node_tuple(node) != pin_tuple(pin):
        refuse(f"{label} locked GitHub source changed")
    if not isinstance(locked.get("lastModified"), int) or locked["lastModified"] < 1:
        refuse(f"{label} has an invalid lastModified value")
    allowed_original = {"owner", "repo", "rev", "ref", "type"}
    if not set(original).issubset(allowed_original):
        refuse(f"{label} original source contains transport-shaping fields")
    if (
        original.get("type") != "github"
        or original.get("owner") != pin["owner"]
        or original.get("repo") != pin["repo"]
    ):
        refuse(f"{label} original GitHub source changed")
    if "rev" in original and original["rev"] != pin["revision"]:
        refuse(f"{label} original revision changed")
    if require_original_revision and original.get("rev") != pin["revision"]:
        refuse(f"{label} original source must use the immutable revision")


def verify_lock_file(
    path: Path,
    expected: dict,
    root_inputs: dict,
    *,
    non_flake_inputs: frozenset[str] = frozenset(),
) -> dict:
    lock = load_json_file(path, f"{path.relative_to(ROOT)} lock")
    if not isinstance(lock, dict):
        refuse(f"{path.relative_to(ROOT)} lock must be a JSON object")
    if set(lock) != {"nodes", "root", "version"} or lock.get("version") != 7:
        refuse(f"{path.relative_to(ROOT)} lock envelope changed")
    nodes = lock.get("nodes", {})
    if set(nodes) != set(expected) | {"root"}:
        refuse(f"{path.relative_to(ROOT)} lock inventory changed")
    if nodes.get("root", {}).get("inputs") != root_inputs:
        refuse(f"{path.relative_to(ROOT)} root input graph changed")
    for name, pin in expected.items():
        node = nodes.get(name, {})
        verify_github_lock_node(
            node,
            pin,
            f"{path.relative_to(ROOT)} lock node {name}",
            require_original_revision=path == FIXTURE / "flake.lock",
            flake=name not in non_flake_inputs,
        )
    return lock


def verify_locks_and_fixture(pins: dict) -> None:
    root_lock = load_json_file(ROOT / "flake.lock", "root flake lock")
    if not isinstance(root_lock, dict):
        refuse("root flake.lock must be a JSON object")
    if set(root_lock) != {"nodes", "root", "version"} or root_lock.get("version") != 7:
        refuse("root flake.lock envelope changed")
    if set(root_lock.get("nodes", {})) != {"brew-src", "root"}:
        refuse("root flake.lock inventory changed")
    root_node = root_lock["nodes"]["brew-src"]
    root_pin = pins["rootLock"]["brew-src"]
    verify_github_lock_node(root_node, root_pin, "root brew-src lock", flake=False)
    if root_node.get("original", {}).get("ref") != root_pin["ref"]:
        refuse("root brew-src release ref changed")
    if root_lock["nodes"]["root"].get("inputs") != {"brew-src": "brew-src"}:
        refuse("root brew-src input graph changed")

    ci_lock = verify_lock_file(
        ROOT / "ci" / "flake.lock",
        pins["ciLocks"],
        {
            "flake-compat": "flake-compat",
            "nix-darwin_26_05": "nix-darwin_26_05",
            "nix-darwin_unstable": "nix-darwin_unstable",
            "nix-github-actions": "nix-github-actions",
            "nixpkgs_26_05": "nixpkgs_26_05",
            "nixpkgs_unstable": "nixpkgs_unstable",
        },
        non_flake_inputs=frozenset({"flake-compat"}),
    )
    if ci_lock["nodes"]["nix-darwin_26_05"].get("inputs") != {"nixpkgs": "nixpkgs"}:
        refuse("nix-darwin 26.05 CI input graph changed")
    if ci_lock["nodes"]["nix-darwin_unstable"].get("inputs") != {
        "nixpkgs": "nixpkgs_2"
    }:
        refuse("nix-darwin unstable CI input graph changed")

    fixture_lock = verify_lock_file(
        FIXTURE / "flake.lock",
        pins["integrationInputs"],
        {
            "homebrew-brew": "homebrew-brew",
            "nix-darwin": "nix-darwin",
            "nix-homebrew": "nix-homebrew",
            "nixpkgs": "nixpkgs",
        },
        non_flake_inputs=frozenset({"homebrew-brew"}),
    )
    if fixture_lock["nodes"]["nix-homebrew"].get("inputs") != {
        "brew-src": ["homebrew-brew"]
    }:
        refuse("nix-homebrew must follow the promoted homebrew-brew input")
    if fixture_lock["nodes"]["nix-darwin"].get("inputs") != {"nixpkgs": ["nixpkgs"]}:
        refuse("nix-darwin must follow the integration nixpkgs input")
    if fixture_lock["nodes"]["homebrew-brew"].get("inputs") is not None:
        refuse("homebrew-brew integration lock gained an input")
    if fixture_lock["nodes"]["nixpkgs"].get("inputs") is not None:
        refuse("nixpkgs integration lock gained an input")

    root_flake = read_regular_text(ROOT / "flake.nix", "root flake")
    if 'url = "github:Homebrew/brew/6.0.22";' not in root_flake:
        refuse("root flake no longer names the locked Homebrew release")

    flake = read_regular_text(FIXTURE / "flake.nix", "integration fixture flake")
    module = read_regular_text(
        FIXTURE / "fleet-darwin.nix", "integration Darwin fixture"
    )
    expected_flake_fragments = (
        "github:axiomlayer/nix-homebrew/" + pins["sourceCommit"],
        "github:axiomlayer/homebrew/" + root_pin["revision"],
        "github:axiomlayer/nix-darwin/"
        + pins["integrationInputs"]["nix-darwin"]["revision"],
        "github:axiomlayer/nixpkgs/" + pins["integrationInputs"]["nixpkgs"]["revision"],
        'inputs.brew-src.follows = "homebrew-brew"',
        "aarch64-darwin",
        "x86_64-darwin",
    )
    for fragment in expected_flake_fragments:
        if fragment not in flake:
            refuse(f"integration fixture lost required fragment: {fragment}")
    for fragment in (
        "nix.enable = false",
        "nix-homebrew =",
        "autoMigrate = false",
        "mutableTaps = false",
        "patchBrew = true",
        "HOMEBREW_NO_ANALYTICS",
        'builtins.pathExists "${brewPath}/Library/Homebrew"',
    ):
        if fragment not in module:
            refuse(f"integration fixture lost required behavior: {fragment}")


def regular_file_inventory(
    root: Path, relative_directory: Path, label: str
) -> set[Path]:
    directory = root / relative_directory
    if directory.is_symlink():
        refuse(f"{label} directory must not be a symlink")
    if not directory.is_dir():
        refuse(f"{label} directory is missing")

    found: set[Path] = set()
    for entry in directory.iterdir():
        relative = entry.relative_to(root)
        if entry.is_symlink():
            refuse(f"{label} entry must not be a symlink: {relative}")
        if not entry.is_file():
            refuse(f"{label} entry must be a regular file: {relative}")
        if entry.stat().st_mode & 0o111:
            refuse(f"{label} entry must not be executable: {relative}")
        found.add(relative)
    return found


def workflow_files(root: Path = ROOT) -> set[Path]:
    return regular_file_inventory(root, Path(".github/workflows"), "active workflow")


def archived_workflow_files(root: Path = ROOT) -> set[Path]:
    return regular_file_inventory(root, ARCHIVE_DIRECTORY, "archived workflow")


def workflow_tree_at_revision(revision: str) -> dict[Path, tuple[str, str]]:
    if not GIT_SHA.fullmatch(revision):
        refuse("upstream workflow candidate must be an immutable commit SHA")
    resolved = run("git", "rev-parse", revision + "^{commit}").stdout.strip()
    if resolved != revision:
        refuse("upstream workflow candidate did not resolve exactly")
    result = run(
        "git",
        "ls-tree",
        "-r",
        revision,
        "--",
        ".github/workflows",
    ).stdout
    observed: dict[Path, tuple[str, str]] = {}
    for line in result.splitlines():
        metadata, raw_path = line.split("\t", 1)
        mode, object_kind, object_id = metadata.split()
        path = Path(raw_path)
        if mode != "100644" or object_kind != "blob":
            refuse(f"upstream workflow is not a regular file: {path}")
        observed[path] = (object_id, mode)
    return observed


def git_blob_bytes(revision: str, path: Path) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{revision}:{path.as_posix()}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        refuse(f"immutable workflow blob is missing: {revision}:{path}")
    return result.stdout


def git_blob_oid(raw: bytes) -> str:
    header = f"blob {len(raw)}\0".encode()
    return hashlib.sha1(header + raw, usedforsecurity=False).hexdigest()


def git_index_entry(path: Path) -> tuple[str, str]:
    result = run("git", "ls-files", "--stage", "--", path.as_posix()).stdout.strip()
    rows = result.splitlines()
    if len(rows) != 1 or "\t" not in rows[0]:
        refuse(f"Git index entry is missing or ambiguous: {path}")
    metadata, observed_path = rows[0].split("\t", 1)
    parts = metadata.split()
    if len(parts) != 3 or parts[2] != "0" or observed_path != path.as_posix():
        refuse(f"Git index entry is malformed: {path}")
    mode, object_id, _stage = parts
    return mode, object_id


def validate_archived_action_references(
    text: str, path: Path, actions: dict
) -> tuple[str, ...]:
    observed: list[str] = []
    for match in ACTION_REFERENCE.finditer(text):
        reference = match.group(1)
        if "@" not in reference:
            refuse(f"{path}: archived action has no reviewed tag: {reference}")
        action, tag = reference.rsplit("@", 1)
        pin = actions.get(action)
        if pin is None or tag != pin.get("tag"):
            refuse(f"{path}: archived action tag is not reviewed: {reference}")
        if not GIT_SHA.fullmatch(pin.get("revision", "")):
            refuse(f"{path}: archived action lacks an immutable commit: {reference}")
        observed.append(reference)
    return tuple(observed)


def verify_upstream_candidate_workflows(revision: str, pins: dict) -> None:
    archives = pins["workflowIsolation"]["archives"]
    expected_sources = {Path(item["sourcePath"]) for item in archives}
    observed = workflow_tree_at_revision(revision)
    if set(observed) != expected_sources:
        refuse(
            "upstream candidate workflow inventory changed at immutable revision "
            f"{revision}: {sorted(map(str, observed))}"
        )
    for item in archives:
        source = Path(item["sourcePath"])
        if observed.get(source) != (item["blobSha1"], item["mode"]):
            refuse(f"upstream workflow mode or blob changed: {source}")
        raw = git_blob_bytes(revision, source)
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            refuse(f"upstream candidate workflow changed: {source}")


def verify_workflow_isolation(pins: dict, root: Path = ROOT) -> None:
    isolation = pins["workflowIsolation"]
    expected_active = {Path(path) for path in isolation["active"]}
    if expected_active != EXPECTED_WORKFLOWS:
        refuse("the active workflow policy changed")
    found_active = workflow_files(root)
    if found_active != expected_active:
        refuse(f"active workflow inventory changed: {sorted(map(str, found_active))}")

    archives = isolation["archives"]
    expected_archives = {Path(item["archivePath"]) for item in archives}
    found_archives = archived_workflow_files(root)
    if found_archives != expected_archives:
        refuse(
            f"archived workflow inventory changed: {sorted(map(str, found_archives))}"
        )

    baseline = isolation["archiveBaseline"]
    archived_action_refs: list[str] = []
    for item in archives:
        source = Path(item["sourcePath"])
        archived = root / item["archivePath"]
        raw = read_regular_bytes(archived, f"archived workflow {archived}")
        if archived.stat().st_mode & 0o111:
            refuse(f"archived workflow must not be executable: {archived}")
        if git_blob_oid(raw) != item["blobSha1"]:
            refuse(f"archived workflow Git blob changed: {archived.relative_to(root)}")
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            refuse(f"archived workflow digest changed: {archived.relative_to(root)}")
        if raw != git_blob_bytes(baseline, source):
            refuse(f"archived workflow differs from {baseline}:{source.as_posix()}")
        archived_action_refs.extend(
            validate_archived_action_references(
                raw.decode("utf-8"), Path(item["archivePath"]), pins["actions"]
            )
        )
        if root.resolve() == ROOT.resolve():
            if git_index_entry(Path(item["archivePath"])) != (
                item["mode"],
                item["blobSha1"],
            ):
                refuse(
                    "archived workflow index mode or blob changed: "
                    f"{item['archivePath']}"
                )

    if tuple(archived_action_refs) != EXPECTED_ARCHIVED_ACTION_REFS:
        refuse(f"archived action inventory changed: {tuple(archived_action_refs)!r}")

    verify_upstream_candidate_workflows(baseline, pins)


def validate_action_references(text: str, path: Path, actions: dict) -> set[str]:
    seen: set[str] = set()
    for match in ACTION_REFERENCE.finditer(text):
        reference = match.group(1)
        if reference.startswith("./"):
            continue
        if "@" not in reference:
            refuse(f"{path}: action has no immutable revision: {reference}")
        action, revision = reference.rsplit("@", 1)
        if not GIT_SHA.fullmatch(revision):
            refuse(f"{path}: {action} is not pinned to a full commit SHA")
        pin = actions.get(action)
        if pin is None or pin.get("revision") != revision:
            refuse(f"{path}: unreviewed action revision {action}@{revision}")
        seen.add(action)
    return seen


def workflow_context_allowed(
    repository: str,
    event_name: str,
    ref: str,
    base_ref: str = "",
    workflow_ref: str = "",
    default_branch: str = "",
    ref_protected: bool = False,
    head_repository: str = "",
    pull_request_number: int | None = None,
    event_sha: str = "",
    merge_commit_sha: str = "",
) -> bool:
    if repository != CANONICAL_REPOSITORY or default_branch != CANONICAL_BRANCH:
        return False
    if event_name in PROACTIVE_EVENTS:
        return (
            ref == CANONICAL_REF
            and ref_protected
            and workflow_ref == CANONICAL_WORKFLOW_REF
        )
    if event_name == "push":
        return (
            ref == CANONICAL_REF
            and ref_protected
            and workflow_ref == CANONICAL_WORKFLOW_REF
        )
    if event_name == "pull_request":
        if not isinstance(pull_request_number, int) or pull_request_number < 1:
            return False
        expected_ref = f"refs/pull/{pull_request_number}/merge"
        expected_workflow_ref = (
            f"{CANONICAL_PR_WORKFLOW_PREFIX}{pull_request_number}/merge"
        )
        return (
            head_repository == CANONICAL_REPOSITORY
            and base_ref == CANONICAL_BRANCH
            and ref == expected_ref
            and workflow_ref == expected_workflow_ref
            and bool(event_sha)
            and event_sha == merge_commit_sha
        )
    return False


def job_if_expression(text: str, job: str) -> str:
    marker = f"  {job}:\n"
    start = text.find(marker)
    if start < 0:
        refuse(f"workflow lost required job {job!r}")
    body = text[start + len(marker) :]
    boundary = re.search(r"^  [A-Za-z0-9_-]+:[ \t]*$", body, re.MULTILINE)
    if boundary:
        body = body[: boundary.start()]
    expressions = re.findall(r"^    if:[ \t]*(.+?)[ \t]*$", body, re.MULTILINE)
    if len(expressions) != 1:
        refuse(f"job {job!r} must have exactly one scalar guard")
    return expressions[0]


def job_body(text: str, job: str) -> str:
    marker = f"  {job}:\n"
    start = text.find(marker)
    if start < 0:
        refuse(f"workflow lost required job {job!r}")
    body = text[start + len(marker) :]
    boundary = re.search(r"^  [A-Za-z0-9_-]+:[ \t]*$", body, re.MULTILINE)
    if boundary:
        body = body[: boundary.start()]
    return body.rstrip()


def verify_integration_trigger(integration: str) -> None:
    trigger = re.search(
        r"^on:\n.*?(?=^[^\s]|\Z)",
        integration,
        re.MULTILINE | re.DOTALL,
    )
    if trigger is None or trigger.group(0).rstrip() != EXPECTED_INTEGRATION_TRIGGER:
        refuse("AxiomLayer integration trigger contract changed")


def verify_integration_job_guards(integration: str) -> None:
    try:
        jobs_text = integration.split("\njobs:\n", 1)[1]
    except IndexError:
        refuse("AxiomLayer integration lost its jobs mapping")
    observed_jobs = tuple(
        re.findall(r"^  ([A-Za-z0-9_-]+):[ \t]*$", jobs_text, re.MULTILINE)
    )
    if observed_jobs != INTEGRATION_JOBS:
        refuse(f"AxiomLayer integration job inventory changed: {observed_jobs!r}")

    for job in INTEGRATION_JOBS[:-1]:
        if job_if_expression(integration, job) != INTEGRATION_CONTEXT_GUARD:
            refuse(f"job {job!r} lacks the exact repository, event, and ref guard")
    if job_if_expression(integration, "integration-gate") != "always()":
        refuse("terminal integration gate must run unconditionally")
    if job_body(integration, "integration-gate") != EXPECTED_TERMINAL_JOB_BODY:
        refuse("terminal integration gate contract changed")


def verify_hosted_runner_contract(integration: str) -> None:
    runs_on = tuple(
        match.strip()
        for match in re.findall(
            r"^    runs-on:[ \t]*(.*?)[ \t]*$", integration, re.MULTILINE
        )
    )
    if runs_on != EXPECTED_RUNS_ON:
        refuse(f"hosted runner inventory changed: {runs_on!r}")
    matrix_runners = tuple(
        re.findall(
            r"^[ \t]+- runner:[ \t]*([^\s#]+)[ \t]*$",
            integration,
            re.MULTILINE,
        )
    )
    if matrix_runners != EXPECTED_MATRIX_RUNNERS:
        refuse(f"hosted Darwin runner inventory changed: {matrix_runners!r}")


def verify_integration_workflow(integration: str) -> None:
    verify_integration_trigger(integration)
    verify_integration_job_guards(integration)
    verify_hosted_runner_contract(integration)

    permission_lines = re.findall(r"^[ \t]*permissions:.*$", integration, re.MULTILINE)
    if permission_lines != ["permissions: {}"]:
        refuse("AxiomLayer integration must give its automatic token no permissions")
    if "uses: actions/checkout@" in integration:
        refuse("AxiomLayer integration must check out the public ref anonymously")
    if "cachix/install-nix-action@" in integration:
        refuse("the integration workflow must not delegate Nix bootstrap to an Action")
    if ACTION_REFERENCE.search(integration):
        refuse("the integration workflow must not delegate execution to an Action")
    if integration.count("run: sh .axiomlayer/install-nix-ci.sh") != 2:
        refuse("each Nix job must use the reviewed credential-scrubbed bootstrap")
    for command in (
        "python3 -I -B .axiomlayer/check.py verify --live",
        "python3 -I -B -m unittest discover -s .axiomlayer -p 'test_*.py' -v",
    ):
        if integration.count(command) != 1:
            refuse(f"isolated Python verification command changed: {command}")
    for fragment in (
        "git init .",
        "SOURCE_SHA: ${{ github.sha }}",
        "SOURCE_URL: https://github.com/axiomlayer/nix-homebrew.git",
        'git remote add origin "$SOURCE_URL"',
        'fetch --no-tags --depth=256 origin "$SOURCE_SHA"',
        'test "$(git rev-parse HEAD)" = "$SOURCE_SHA"',
    ):
        if integration.count(fragment) != 3:
            refuse(f"anonymous checkout contract changed: {fragment}")
    for fragment in (
        "self-hosted",
        "secrets.",
        "secrets: inherit",
        "persist-credentials: true",
        "id-token: write",
        "contents: write",
        "actions: write",
        "checks: write",
        "deployments: write",
        "discussions: write",
        "issues: write",
        "packages: write",
        "pages: write",
        "pull-requests: write",
        "security-events: write",
        "statuses: write",
        "permissions: write-all",
        "environment:",
        "gh release",
        "gh api",
        "git push",
        "git commit",
        "git tag",
        "nix copy",
        "cachix push",
        "docker push",
        "npm publish",
        "twine upload",
        "continue-on-error:",
        "set +e",
        "|| true",
        "darwin-rebuild",
        "launchctl",
        "osascript",
        "sudo ",
        "/activate",
        "brew install",
        "github.token",
        "github_access_token",
    ):
        if fragment in integration.lower():
            refuse(f"AxiomLayer integration contains a mutating capability: {fragment}")


def verify_workflows(pins: dict) -> None:
    verify_workflow_isolation(pins)
    integration = read_regular_text(
        ROOT / ACTIVE_WORKFLOW, "active AxiomLayer integration workflow"
    )
    verify_integration_workflow(integration)


def verify_brew_tree_contract(
    locked_paths: set[str], promoted_paths: set[str], pins: dict
) -> None:
    required = set(pins["promotedHomebrew"]["requiredPaths"])
    if not required.issubset(locked_paths):
        refuse("the locked Homebrew source no longer has the runtime tree")
    observed_missing = sorted(required - promoted_paths)
    if observed_missing != pins["promotedHomebrew"]["missingPaths"]:
        refuse("the promoted Homebrew tree no longer matches its quarantine record")
    if "bin/brew" not in promoted_paths:
        refuse("the promoted migration tree unexpectedly lost its bootstrap shim")
    if "Library/Homebrew" in promoted_paths:
        refuse("the promoted Homebrew source is no longer safely quarantined")


def verify_nix_bootstrap(nix_installer_path: Path, nix_pin: dict) -> None:
    raw_installer = read_regular_bytes(
        nix_installer_path, "credential-scrubbed Nix bootstrap"
    )
    nix_installer = raw_installer.decode("utf-8")
    if hashlib.sha256(raw_installer).hexdigest() != nix_pin["wrapperSha256"]:
        refuse("the credential-scrubbed Nix bootstrap digest changed")
    for required in (
        f"NIX_VERSION={nix_pin['version']}",
        f"INSTALLER_URL={nix_pin['installUrl']}",
        f"INSTALLER_SHA256={nix_pin['installerSha256']}",
        "env -i",
        "--no-channel-add --no-modify-profile",
        'temporary_parent=$(CDPATH= cd -P -- "$temporary_parent_input" && pwd -P)',
        'temporary_directory=$(CDPATH= cd -P -- "$temporary_directory" && pwd -P)',
        'if [ "$temporary_observed_parent" != "$temporary_parent" ]; then',
        'if [ ! -d "$temporary_directory" ] || [ -L "$temporary_directory" ]; then',
    ):
        if required not in nix_installer:
            refuse(f"Nix bootstrap lost required boundary: {required}")
    for digest in nix_pin["binaryTarballSha256"].values():
        if digest not in nix_installer:
            refuse("Nix bootstrap lost a pinned platform digest")
    for forbidden in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "github.token",
        "github_access_token",
    ):
        if forbidden in nix_installer:
            refuse(f"Nix bootstrap references a live credential surface: {forbidden}")


def verify_integration_files() -> None:
    pins = load_pins()
    for relative, expected_digest in pins["integrationFileSha256"].items():
        raw = read_regular_bytes(
            ROOT / relative, f"reviewed integration file {relative}"
        )
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            refuse(f"reviewed integration file digest changed: {relative}")
    gate = read_regular_text(
        INTEGRATION / "run-nix-homebrew-gate.sh", "Nix evaluation gate"
    )
    verify_nix_bootstrap(INTEGRATION / "install-nix-ci.sh", pins["nix"])
    for mutator in (
        "darwin-rebuild",
        "/activate",
        "launchctl",
        "osascript",
        "brew install",
        "nix profile install",
    ):
        if mutator in gate.lower():
            refuse(f"host mutation escaped into the hosted gate: {mutator}")
    if "promoted_brew=quarantined" not in gate:
        refuse("the promoted Homebrew refusal is not an explicit gate outcome")
    for required in (
        "env -i",
        "--option accept-flake-config false",
        "--option allow-import-from-derivation false",
        '--option flake-registry ""',
        "--option sandbox true",
        "--option sandbox-fallback false",
        "promoted-homebrew-revision",
        'scratch_parent=$(CDPATH= cd -P -- "$scratch_parent_input" && pwd -P)',
        'scratch=$(CDPATH= cd -P -- "$scratch" && pwd -P)',
        'if [ "$scratch_observed_parent" != "$scratch_parent" ]; then',
        'if [ ! -d "$scratch" ] || [ -L "$scratch" ]; then',
    ):
        if required not in gate:
            refuse(f"Nix gate lost required isolation control: {required}")

    forbidden_credentials = (
        "ghp" + "_",
        "github" + "_pat_",
        "begin " + "private key",
        "cache-signing" + "-key",
    )
    for path in INTEGRATION.rglob("*"):
        if path.is_symlink():
            refuse(f"integration path must not be a symlink: {path.relative_to(ROOT)}")
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").lower()
        except UnicodeDecodeError:
            continue
        for fragment in forbidden_credentials:
            if fragment in text:
                refuse(f"credential material appeared in {path.relative_to(ROOT)}")


def verify_machine_identity_text(text: str, path: Path) -> None:
    uppercase_namespace = "".join(("Axiom", "Layer", "/"))
    if uppercase_namespace in text:
        refuse(f"{path}: machine identity must use the canonical lowercase namespace")


def tracked_text_files() -> list[tuple[Path, str]]:
    paths = run("git", "ls-files", "-z").stdout.split("\0")
    result: list[tuple[Path, str]] = []
    for raw_path in paths:
        if not raw_path:
            continue
        path = ROOT / raw_path
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        result.append((path, text))
    return result


def verify_machine_identities() -> None:
    for path, text in tracked_text_files():
        verify_machine_identity_text(text, path.relative_to(ROOT))


def verify_runtime_dependency_text(text: str, path: Path) -> None:
    prohibited = ("jsr" + ":", "jsr" + ".io")
    lowered = text.lower()
    for fragment in prohibited:
        if fragment in lowered:
            refuse(f"runtime dependency on prohibited registry in {path}")


def verify_runtime_dependency_absent() -> None:
    for path, text in tracked_text_files():
        verify_runtime_dependency_text(text, path.relative_to(ROOT))


def verify_retired_surface_absent() -> None:
    retired = (
        "_".join(("codex", "security", "gate")),
        "-".join(("codex", "security", "gate")),
    )
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for spelling in retired:
            if spelling in text.lower():
                refuse(
                    f"retired security surface reappeared in {path.relative_to(ROOT)}"
                )


def changed_paths(pins: dict) -> set[str]:
    commands = (
        ("git", "diff", "--name-only", pins["sourceCommit"] + "...HEAD"),
        ("git", "diff", "--name-only"),
        ("git", "diff", "--cached", "--name-only"),
        ("git", "ls-files", "--others", "--exclude-standard"),
    )
    paths: set[str] = set()
    for command in commands:
        paths.update(line for line in run(*command).stdout.splitlines() if line)
    return paths


def allowed_integration_path(path: str) -> bool:
    return (
        path.startswith(".axiomlayer/")
        or path.startswith(".github/upstream-workflows/")
        or path
        in {
            ".github/workflows/axiomlayer-integration.yml",
            ".github/workflows/ci.yaml",
        }
    )


def verify_local_source(pins: dict) -> None:
    run("git", "cat-file", "-e", pins["sourceCommit"] + "^{commit}")
    source_tree = run(
        "git", "show", "-s", "--format=%T", pins["sourceCommit"]
    ).stdout.strip()
    if source_tree != pins["sourceTree"]:
        refuse("the authoritative local nix-homebrew source tree changed")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", pins["sourceCommit"], "HEAD"],
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        refuse("the integration branch is not descended from the exact source commit")
    unexpected = sorted(
        path for path in changed_paths(pins) if not allowed_integration_path(path)
    )
    if unexpected:
        refuse(f"writes escaped the integration boundary: {unexpected}")


def download(url: str, limit: int = 8 * 1024 * 1024) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "AxiomLayer-nix-homebrew-integration"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        refuse(f"download exceeded the reviewed size ceiling: {url}")
    return data


def ls_remote(url: str, ref: str) -> str:
    result = run("git", "ls-remote", url, ref)
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != 1 or rows[0][1] != ref:
        refuse(f"{url} does not expose exactly one {ref}")
    return rows[0][0]


def ls_remote_tag(url: str, tag: str) -> str:
    ref = f"refs/tags/{tag}"
    peeled_ref = ref + "^{}"
    result = run("git", "ls-remote", url, ref, peeled_ref)
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    observed = {row[1]: row[0] for row in rows if len(row) == 2}
    if set(observed) not in ({ref}, {ref, peeled_ref}):
        refuse(f"{url} does not expose exactly one reviewed tag {tag}")
    revision = observed.get(peeled_ref, observed.get(ref, ""))
    if not GIT_SHA.fullmatch(revision):
        refuse(f"{url} tag {tag} did not resolve to an immutable commit")
    return revision


def archive_paths(archive: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as stream:
        roots: set[str] = set()
        paths: set[str] = set()
        for member in stream.getmembers():
            root, separator, relative = member.name.partition("/")
            roots.add(root)
            if separator and relative:
                paths.add(relative.rstrip("/"))
    if len(roots) != 1:
        refuse("source archive does not have exactly one root directory")
    return paths


def git_at(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def verify_remote_descendant(url: str, ref: str, base: str) -> None:
    expected_head = ls_remote(url, ref)
    with tempfile.TemporaryDirectory(prefix="axiom-nix-homebrew-history-") as temporary:
        repository = Path(temporary) / "history.git"
        subprocess.run(
            ["git", "init", "--bare", "--quiet", str(repository)], check=True
        )
        git_at(
            repository,
            "fetch",
            "--quiet",
            "--no-tags",
            "--depth=256",
            url,
            f"{ref}:refs/check/head",
        )
        observed_head = git_at(repository, "rev-parse", "refs/check/head^{commit}")
        if observed_head != expected_head:
            refuse(f"{url} moved while {ref} ancestry was being checked")
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "merge-base",
                "--is-ancestor",
                base,
                observed_head,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            refuse(f"{url} {ref} is not descended from {base} within 256 commits")


def homebrew_relationship(pins: dict) -> dict:
    locked = pins["rootLock"]["brew-src"]["revision"]
    promoted = pins["promotedHomebrew"]["revision"]
    url = "https://github.com/axiomlayer/homebrew.git"
    if ls_remote_tag(url, "6.0.22") != locked:
        refuse("axiomlayer/homebrew tag 6.0.22 moved from the locked commit")
    if ls_remote(url, "refs/heads/master") != promoted:
        refuse("axiomlayer/homebrew legacy master moved from the promoted commit")

    with tempfile.TemporaryDirectory(prefix="axiom-homebrew-history-") as temporary:
        repository = Path(temporary) / "history.git"
        subprocess.run(
            ["git", "init", "--bare", "--quiet", str(repository)], check=True
        )
        git_at(
            repository,
            "fetch",
            "--quiet",
            "--no-tags",
            "--depth=128",
            url,
            "refs/tags/6.0.22:refs/tags/6.0.22",
            "refs/heads/master:refs/heads/master",
        )
        merge_base = git_at(repository, "merge-base", locked, promoted)
        counts = git_at(
            repository, "rev-list", "--left-right", "--count", locked + "..." + promoted
        ).split()
    if len(counts) != 2:
        refuse("could not calculate the locked/promoted Homebrew relationship")
    behind, ahead = (int(value) for value in counts)
    if behind and ahead:
        status = "diverged"
    elif ahead:
        status = "ahead"
    elif behind:
        status = "behind"
    else:
        status = "identical"
    return {
        "status": status,
        "aheadBy": ahead,
        "behindBy": behind,
        "mergeBase": merge_base,
    }


def verify_policy_snapshot(pins: dict) -> None:
    fork_evidence = pins["forkEvidence"]
    raw_fork = read_regular_bytes(
        ROOT / fork_evidence["path"], "true-fork evidence snapshot"
    )
    if hashlib.sha256(raw_fork).hexdigest() != fork_evidence["sha256"]:
        refuse("the recorded true-fork evidence bytes changed")
    if parse_json_bytes(raw_fork, "true-fork evidence snapshot") != {
        "defaultBranch": "main",
        "fork": True,
        "fullName": "axiomlayer/nix-homebrew",
        "parent": "zhaofengli/nix-homebrew",
        "visibility": "public",
    }:
        refuse("the recorded repository metadata is not the exact true fork")

    policy = pins["policySnapshot"]
    raw_policy = read_regular_bytes(
        ROOT / policy["evidencePath"], "upstream policy evidence snapshot"
    )
    if hashlib.sha256(raw_policy).hexdigest() != policy["sha256"]:
        refuse("the recorded 18-source policy bytes changed")
    parsed = parse_json_bytes(raw_policy, "upstream policy evidence snapshot")
    if not isinstance(parsed, dict):
        refuse("upstream policy evidence snapshot must be a JSON object")
    sources = parsed.get("sources", [])
    if len(sources) != policy["sourceCount"]:
        refuse("the recorded central source count changed")
    if policy["missingSource"] in {source.get("id") for source in sources}:
        refuse("nix-homebrew is no longer missing from the recorded policy snapshot")

    promoted = pins["promotedHomebrew"]
    raw_homebrew = read_regular_bytes(
        ROOT / promoted["policyEvidencePath"], "Homebrew policy evidence snapshot"
    )
    if hashlib.sha256(raw_homebrew).hexdigest() != promoted["policySha256"]:
        refuse("the recorded Homebrew policy bytes changed")
    homebrew = parse_json_bytes(raw_homebrew, "Homebrew policy evidence snapshot")
    if not isinstance(homebrew, dict):
        refuse("Homebrew policy evidence snapshot must be a JSON object")
    observed = homebrew.get("brew", {})
    if (
        observed.get("repository") != promoted["repository"]
        or observed.get("commit") != promoted["revision"]
    ):
        refuse("the recorded Homebrew promotion does not match the quarantine pin")


def verify_live_provenance(pins: dict) -> None:
    source = pins["sourceCommit"]
    verify_remote_descendant(
        "https://github.com/zhaofengli/nix-homebrew.git",
        "refs/heads/main",
        source,
    )
    verify_remote_descendant(
        "https://github.com/axiomlayer/nix-homebrew.git",
        "refs/heads/main",
        source,
    )

    for action, pin in pins["actions"].items():
        if ls_remote_tag(pin["repositoryUrl"], pin["tag"]) != pin["revision"]:
            refuse(f"{action} tag moved from its reviewed commit")

    nix_pin = pins["nix"]
    nix_url = f"https://github.com/{nix_pin['sourceRepository']}.git"
    if ls_remote_tag(nix_url, nix_pin["sourceTag"]) != nix_pin["sourceCommit"]:
        refuse("official Nix release tag moved from its reviewed commit")
    installer = download(nix_pin["installUrl"])
    if hashlib.sha256(installer).hexdigest() != nix_pin["installerSha256"]:
        refuse("official Nix installer digest changed")
    for digest in nix_pin["binaryTarballSha256"].values():
        if installer.count(f"hash={digest}".encode()) != 1:
            refuse("official Nix installer platform digest inventory changed")

    archive = download(pins["sourceArchive"]["url"])
    if hashlib.sha256(archive).hexdigest() != pins["sourceArchive"]["sha256"]:
        refuse("the AxiomLayer nix-homebrew source archive digest changed")
    source_paths = archive_paths(archive)
    if not {"flake.nix", "flake.lock", "modules/default.nix"}.issubset(source_paths):
        refuse("the exact nix-homebrew source archive lost its module surface")

    root = pins["rootLock"]["brew-src"]
    promoted = pins["promotedHomebrew"]
    if homebrew_relationship(pins) != promoted["relationshipToLocked"]:
        refuse("the locked/promoted Homebrew ancestry relationship changed")

    locked_archive = download(root["archiveUrl"])
    if hashlib.sha256(locked_archive).hexdigest() != root["archiveSha256"]:
        refuse("the locked Homebrew source archive digest changed")
    promoted_archive = download(promoted["archiveUrl"])
    if hashlib.sha256(promoted_archive).hexdigest() != promoted["archiveSha256"]:
        refuse("the promoted Homebrew source archive digest changed")
    locked_paths = archive_paths(locked_archive)
    promoted_paths = archive_paths(promoted_archive)
    verify_brew_tree_contract(locked_paths, promoted_paths, pins)


def safe_extract_git_archive(revision: str, destination: Path) -> str:
    if destination.exists():
        refuse(f"materialization destination already exists: {destination}")
    if destination.parent.resolve() == Path("/"):
        refuse("refusing a broad materialization destination")
    destination.mkdir(parents=True)
    resolved_destination = destination.resolve()

    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        members = stream.getmembers()
        for member in members:
            target = (resolved_destination / member.name).resolve()
            if (
                resolved_destination not in target.parents
                and target != resolved_destination
            ):
                refuse(f"archive path escapes destination: {member.name}")
            if member.issym() or member.islnk():
                refuse(f"archive links are not accepted: {member.name}")
            if not (member.isdir() or member.isreg()):
                refuse(f"archive special files are not accepted: {member.name}")
        stream.extractall(resolved_destination, members=members, filter="data")
    return run("git", "rev-parse", revision + "^{commit}").stdout.strip()


def materialize(pins: dict, revision_kind: str, destination: Path) -> None:
    if revision_kind == "locked":
        revision = pins["sourceCommit"]
        workflow_revision = revision
    elif revision_kind == "candidate":
        revision = run("git", "rev-parse", "HEAD^{commit}").stdout.strip()
        workflow_revision = pins["workflowIsolation"]["archiveBaseline"]
    elif revision_kind == "upstream-main":
        upstream_url = "https://github.com/zhaofengli/nix-homebrew.git"
        expected = ls_remote(upstream_url, "refs/heads/main")
        run(
            "git",
            "fetch",
            "--no-tags",
            "--depth=1",
            upstream_url,
            "refs/heads/main",
        )
        revision = "FETCH_HEAD"
        fetched = run("git", "rev-parse", revision + "^{commit}").stdout.strip()
        if fetched != expected:
            refuse("upstream main moved while the candidate was materialized")
        workflow_revision = fetched
    else:
        refuse(f"unsupported materialization kind: {revision_kind}")
    verify_upstream_candidate_workflows(workflow_revision, pins)
    resolved = safe_extract_git_archive(revision, destination)
    print(
        f"materialized_revision={resolved} kind={revision_kind} "
        f"workflow_revision={workflow_revision} destination={destination}"
    )


def verify(live: bool) -> dict:
    pins = load_pins()
    verify_pins(pins)
    verify_locks_and_fixture(pins)
    verify_workflows(pins)
    verify_integration_files()
    verify_policy_snapshot(pins)
    verify_machine_identities()
    verify_runtime_dependency_absent()
    verify_retired_surface_absent()
    verify_local_source(pins)
    if live:
        verify_live_provenance(pins)
    mode = "static+live" if live else "static"
    print(
        "integration_contract=verified "
        f"mode={mode} source={pins['sourceCommit']} systems={len(pins['systems'])} "
        f"central_source_count={pins['policySnapshot']['sourceCount']} "
        f"promoted_homebrew={pins['promotedHomebrew']['disposition']}"
    )
    return pins


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("verify", "materialize", "promoted-homebrew-revision"),
        nargs="?",
        default="verify",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--revision",
        choices=("locked", "candidate", "upstream-main"),
        default="locked",
    )
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "verify":
            verify(args.live)
        elif args.command == "materialize":
            if args.destination is None:
                parser.error("materialize requires --destination")
            materialize(load_pins(), args.revision, args.destination)
        else:
            if args.destination is not None or args.live:
                parser.error("promoted-homebrew-revision takes no options")
            print(load_pins()["promotedHomebrew"]["revision"])
    except (
        ContractError,
        json.JSONDecodeError,
        OSError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"integration contract refused: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
