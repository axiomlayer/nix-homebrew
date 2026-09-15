# AxiomLayer nix-homebrew integration

This directory is the read-only integration boundary for the true
`axiomlayer/nix-homebrew` fork. The source lane begins at Margay's exact lock:

- upstream: `zhaofengli/nix-homebrew`
- source commit: `09a921d0181146cf6163ec2cc1db7b6fd539a885`
- source NAR hash: `sha256-fEaFq0XgpgWFPLfpq1UK4/8ylHd2T0+fo6O3hKz1LUM=`
- locked Homebrew release: `6.0.22` at
  `08e85c4e42f5d8f1ea17c36cb59cf61c2ccb26c3`

The fixture preserves Margay's input relationship: `nix-homebrew.brew-src`
follows the separately promoted `homebrew-brew` input. It evaluates the module
for both `aarch64-darwin` and `x86_64-darwin`, then hosted runners build only a
small JSON contract. No generated Darwin closure is activated.

## Current promotion quarantine

Dotfiles pull request 49 currently records 18 upstream sources and omits
`nix-homebrew`. Its promoted `axiomlayer/homebrew` revision is
`67984c752d13f3bbcb9aba059d727930aec887dc`. That revision belongs to
Homebrew's legacy migration-only `master` line: it diverges from the locked
6.0.22 runtime revision and has `bin/brew`, but not `Library/Homebrew`.

The integration therefore proves two things independently:

1. the exact nix-homebrew source and pull-request candidate evaluate against
   the complete locked Homebrew tree on both Darwin architectures; and
2. substituting the centrally promoted revision is rejected by a named Nix
   assertion for the recorded missing runtime path.

The second proof is an intentional quarantine, not an expected red workflow.
If the central pin becomes a valid runtime tree, this gate refuses the stale
quarantine record and requires an explicit lock review.

The hosted integration runs once each day and can also be dispatched manually.
Push, schedule, and manual runs require the exact lowercase repository identity,
the recorded default branch, its protected `main` ref, and the exact workflow file
on that ref. Pull requests must target `main` and execute this same repository's
synthetic merge ref, with the merge SHA and workflow ref bound to the exact
pull-request number. Copied workflows, external-fork pull requests, unprotected
or non-default refs, and unrecognized events fail the unconditional terminal
gate red.

## Fork isolation

The inherited CI performs real nix-darwin activation on hosted Macs, so it is not
left executable in this fork. The exact bytes from baseline
`09a921d0181146cf6163ec2cc1db7b6fd539a885` are quarantined as
`.github/upstream-workflows/ci.yaml.disabled` with SHA-256
`a70aabe038a1e85133d6168ba774a8d5af30ff6a4ea67f786f72f8eca89cc5f6`.
The verifier refuses missing, extra, renamed, symlinked, or changed archive files
and any second active workflow. Upstream-candidate audits still read the original
`.github/workflows/ci.yaml` path directly from an immutable Git revision and fail
closed if its inventory or bytes move without review.

The sole active AxiomLayer workflow grants its automatic token no permissions and
checks out the public ref anonymously. Its Nix bootstrap first verifies the
official 2.35.2 launcher SHA-256 and embedded platform tarball digest, then
executes those reviewed bytes under `env -i`; no GitHub or Actions credential
reaches the installer. There are no release, Pages, cache-signing, publisher,
enrollment, or deployment jobs. Archived action tags are resolved live to their
reviewed commit SHAs, and Nix evaluation forces sandboxing while refusing
flake-provided configuration and registries. Disposable directories are created
under a physically resolved temporary parent and then checked by physical direct
parent, so macOS's `/var` alias remains portable without weakening containment or
symlink refusal.

Run the local contract and live provenance checks with:

```console
python3 -I -B .axiomlayer/check.py verify --live
python3 -I -B -m unittest discover -s .axiomlayer -p 'test_*.py' -v
bash -n .axiomlayer/run-nix-homebrew-gate.sh
```

Nix evaluation requires the fleet-pinned Nix 2.35.2. CI performs that proof
when Nix is not present on the authoring host.

## Deliberately hosted-only

Passing this lane does not activate Margay, converge Homebrew packages, render
GUI applications, enroll Margay's self-runner, or prove cold-boot resume. Those
remain explicit physical-Margay acceptance gates.
