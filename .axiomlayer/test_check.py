#!/usr/bin/env python3

from __future__ import annotations

import copy
import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import check  # noqa: E402


class IntegrationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pins = check.load_pins()

    def integration_workflow(self) -> str:
        return (
            check.ROOT / ".github" / "workflows" / "axiomlayer-integration.yml"
        ).read_text(encoding="utf-8")

    def copy_workflow_isolation(self, destination: Path) -> None:
        isolation = self.pins["workflowIsolation"]
        paths = [Path(path) for path in isolation["active"]]
        paths.extend(Path(item["archivePath"]) for item in isolation["archives"])
        for relative in paths:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(check.ROOT / relative, target)

    def test_complete_static_contract(self) -> None:
        check.verify(False)

    def test_missing_archived_workflow_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="axiom-workflow-isolation-") as root:
            candidate = Path(root)
            self.copy_workflow_isolation(candidate)
            archive = Path(self.pins["workflowIsolation"]["archives"][0]["archivePath"])
            (candidate / archive).unlink()
            with self.assertRaisesRegex(check.ContractError, "archive.*inventory"):
                check.verify_workflow_isolation(self.pins, candidate)

    def test_extra_archived_workflow_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="axiom-workflow-isolation-") as root:
            candidate = Path(root)
            self.copy_workflow_isolation(candidate)
            extra = candidate / ".github/upstream-workflows/extra.yml.disabled"
            extra.write_text("name: unexpected\n", encoding="utf-8")
            with self.assertRaisesRegex(check.ContractError, "archive.*inventory"):
                check.verify_workflow_isolation(self.pins, candidate)

    def test_renamed_archived_workflow_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="axiom-workflow-isolation-") as root:
            candidate = Path(root)
            self.copy_workflow_isolation(candidate)
            archive = (
                candidate / self.pins["workflowIsolation"]["archives"][0]["archivePath"]
            )
            archive.rename(archive.with_name("renamed.yaml.disabled"))
            with self.assertRaisesRegex(check.ContractError, "archive.*inventory"):
                check.verify_workflow_isolation(self.pins, candidate)

    def test_symlinked_archived_workflow_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="axiom-workflow-isolation-") as root:
            candidate = Path(root)
            self.copy_workflow_isolation(candidate)
            archive = (
                candidate / self.pins["workflowIsolation"]["archives"][0]["archivePath"]
            )
            archive.unlink()
            archive.symlink_to(check.ROOT / ".github/dependabot.yml")
            with self.assertRaisesRegex(check.ContractError, "must not be a symlink"):
                check.verify_workflow_isolation(self.pins, candidate)

    def test_tampered_archived_workflow_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="axiom-workflow-isolation-") as root:
            candidate = Path(root)
            self.copy_workflow_isolation(candidate)
            archive = (
                candidate / self.pins["workflowIsolation"]["archives"][0]["archivePath"]
            )
            archive.write_bytes(archive.read_bytes() + b"\n# tampered\n")
            with self.assertRaisesRegex(check.ContractError, "changed"):
                check.verify_workflow_isolation(self.pins, candidate)

    def test_executable_archived_workflow_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="axiom-workflow-isolation-") as root:
            candidate = Path(root)
            self.copy_workflow_isolation(candidate)
            archive = (
                candidate / self.pins["workflowIsolation"]["archives"][0]["archivePath"]
            )
            archive.chmod(0o755)
            with self.assertRaisesRegex(check.ContractError, "must not be executable"):
                check.verify_workflow_isolation(self.pins, candidate)

    def test_extra_active_workflow_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="axiom-workflow-isolation-") as root:
            candidate = Path(root)
            self.copy_workflow_isolation(candidate)
            extra = candidate / ".github/workflows/extra.yml"
            extra.write_text("name: unexpected\n", encoding="utf-8")
            with self.assertRaisesRegex(
                check.ContractError, "active workflow inventory"
            ):
                check.verify_workflow_isolation(self.pins, candidate)

    def test_upstream_workflows_are_audited_at_an_immutable_revision(self) -> None:
        check.verify_upstream_candidate_workflows(
            self.pins["workflowIsolation"]["archiveBaseline"], self.pins
        )
        with self.assertRaisesRegex(check.ContractError, "immutable commit SHA"):
            check.verify_upstream_candidate_workflows("HEAD", self.pins)

    def test_expected_workflow_contexts_are_allowed(self) -> None:
        accepted = (
            (
                "schedule",
                check.CANONICAL_REF,
                "",
                check.CANONICAL_WORKFLOW_REF,
                True,
            ),
            (
                "workflow_dispatch",
                check.CANONICAL_REF,
                "",
                check.CANONICAL_WORKFLOW_REF,
                True,
            ),
            (
                "push",
                check.CANONICAL_REF,
                "",
                check.CANONICAL_WORKFLOW_REF,
                True,
            ),
            (
                "pull_request",
                "refs/pull/41/merge",
                check.CANONICAL_BRANCH,
                check.CANONICAL_PR_WORKFLOW_PREFIX + "41/merge",
                False,
            ),
        )
        for event_name, ref, base_ref, workflow_ref, ref_protected in accepted:
            with self.subTest(
                event_name=event_name,
                ref=ref,
                base_ref=base_ref,
                workflow_ref=workflow_ref,
                ref_protected=ref_protected,
            ):
                kwargs = {}
                if event_name == "pull_request":
                    kwargs = {
                        "head_repository": check.CANONICAL_REPOSITORY,
                        "pull_request_number": 41,
                        "event_sha": "a" * 40,
                        "merge_commit_sha": "a" * 40,
                    }
                self.assertTrue(
                    check.workflow_context_allowed(
                        check.CANONICAL_REPOSITORY,
                        event_name,
                        ref,
                        base_ref,
                        workflow_ref,
                        check.CANONICAL_BRANCH,
                        ref_protected,
                        **kwargs,
                    )
                )

    def test_external_or_mismatched_pull_request_is_refused(self) -> None:
        valid = {
            "repository": check.CANONICAL_REPOSITORY,
            "event_name": "pull_request",
            "ref": "refs/pull/41/merge",
            "base_ref": check.CANONICAL_BRANCH,
            "workflow_ref": check.CANONICAL_PR_WORKFLOW_PREFIX + "41/merge",
            "default_branch": check.CANONICAL_BRANCH,
            "head_repository": check.CANONICAL_REPOSITORY,
            "pull_request_number": 41,
            "event_sha": "a" * 40,
            "merge_commit_sha": "a" * 40,
        }
        mutations = (
            {"head_repository": "attacker/nix-homebrew"},
            {"pull_request_number": 42},
            {"event_sha": "b" * 40},
            {"merge_commit_sha": ""},
            {"workflow_ref": check.CANONICAL_WORKFLOW_REF},
            {"ref": "refs/pull/41/head"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                candidate = valid | mutation
                self.assertFalse(check.workflow_context_allowed(**candidate))

    def test_wrong_repository_ref_and_event_are_refused(self) -> None:
        refused = (
            (
                "zhaofengli/nix-homebrew",
                "schedule",
                check.CANONICAL_REF,
                "",
                check.CANONICAL_WORKFLOW_REF,
                check.CANONICAL_BRANCH,
                True,
            ),
            (
                check.CANONICAL_REPOSITORY,
                "schedule",
                "refs/heads/topic",
                "",
                check.CANONICAL_WORKFLOW_REF,
                check.CANONICAL_BRANCH,
                True,
            ),
            (
                check.CANONICAL_REPOSITORY,
                "workflow_dispatch",
                "refs/tags/v1.0.0",
                "",
                check.CANONICAL_WORKFLOW_REF,
                check.CANONICAL_BRANCH,
                True,
            ),
            (
                check.CANONICAL_REPOSITORY,
                "push",
                "refs/heads/topic",
                "",
                check.CANONICAL_WORKFLOW_REF,
                check.CANONICAL_BRANCH,
                True,
            ),
            (
                check.CANONICAL_REPOSITORY,
                "push",
                check.CANONICAL_REF,
                "",
                check.CANONICAL_WORKFLOW_REF,
                check.CANONICAL_BRANCH,
                False,
            ),
            (
                check.CANONICAL_REPOSITORY,
                "push",
                check.CANONICAL_REF,
                "",
                "axiomlayer/nix-homebrew/.github/workflows/other.yml@refs/heads/main",
                check.CANONICAL_BRANCH,
                True,
            ),
            (
                check.CANONICAL_REPOSITORY,
                "push",
                check.CANONICAL_REF,
                "",
                check.CANONICAL_WORKFLOW_REF,
                "develop",
                True,
            ),
            (
                check.CANONICAL_REPOSITORY,
                "pull_request",
                "refs/pull/41/head",
                check.CANONICAL_BRANCH,
                "",
                check.CANONICAL_BRANCH,
                False,
            ),
            (
                check.CANONICAL_REPOSITORY,
                "pull_request",
                "refs/pull/41/merge",
                "develop",
                "",
                check.CANONICAL_BRANCH,
                False,
            ),
            (
                check.CANONICAL_REPOSITORY,
                "pull_request_target",
                check.CANONICAL_REF,
                check.CANONICAL_BRANCH,
                "",
                check.CANONICAL_BRANCH,
                True,
            ),
            (
                check.CANONICAL_REPOSITORY,
                "repository_dispatch",
                check.CANONICAL_REF,
                "",
                "",
                check.CANONICAL_BRANCH,
                True,
            ),
        )
        for (
            repository,
            event_name,
            ref,
            base_ref,
            workflow_ref,
            default_branch,
            ref_protected,
        ) in refused:
            with self.subTest(
                repository=repository,
                event_name=event_name,
                ref=ref,
                base_ref=base_ref,
                workflow_ref=workflow_ref,
            ):
                self.assertFalse(
                    check.workflow_context_allowed(
                        repository,
                        event_name,
                        ref,
                        base_ref,
                        workflow_ref,
                        default_branch,
                        ref_protected,
                    )
                )

    def test_uppercase_machine_identity_is_refused(self) -> None:
        uppercase_namespace = "".join(("Axiom", "Layer", "/"))
        uppercase_repository = uppercase_namespace + "nix-homebrew"
        self.assertFalse(
            check.workflow_context_allowed(
                uppercase_repository,
                "push",
                check.CANONICAL_REF,
                workflow_ref=check.CANONICAL_WORKFLOW_REF,
                default_branch=check.CANONICAL_BRANCH,
                ref_protected=True,
            )
        )
        with self.assertRaisesRegex(check.ContractError, "canonical lowercase"):
            check.verify_machine_identity_text(
                '{"repository": "' + uppercase_repository + '"}',
                Path("uppercase.json"),
            )

        workflow = self.integration_workflow()
        candidate = workflow.replace(check.CANONICAL_REPOSITORY, uppercase_repository)
        self.assertNotEqual(candidate, workflow)
        with self.assertRaisesRegex(check.ContractError, "exact repository"):
            check.verify_integration_workflow(candidate)

    def test_wrong_manual_workflow_ref_is_refused(self) -> None:
        wrong_refs = (
            "",
            "axiomlayer/nix-homebrew/.github/workflows/"
            "axiomlayer-integration.yml@refs/heads/topic",
            "axiomlayer/nix-homebrew/.github/workflows/other.yml@refs/heads/main",
        )
        for event_name in check.PROACTIVE_EVENTS:
            for workflow_ref in wrong_refs:
                with self.subTest(event_name=event_name, workflow_ref=workflow_ref):
                    self.assertFalse(
                        check.workflow_context_allowed(
                            check.CANONICAL_REPOSITORY,
                            event_name,
                            check.CANONICAL_REF,
                            workflow_ref=workflow_ref,
                            default_branch=check.CANONICAL_BRANCH,
                            ref_protected=True,
                        )
                    )

        workflow = self.integration_workflow()
        candidate = workflow.replace(
            check.CANONICAL_WORKFLOW_REF,
            wrong_refs[1],
        )
        self.assertNotEqual(candidate, workflow)
        with self.assertRaisesRegex(check.ContractError, "exact repository"):
            check.verify_integration_workflow(candidate)

    def test_guard_mutations_are_refused(self) -> None:
        workflow = self.integration_workflow()
        mutations = (
            (
                "github.repository == 'axiomlayer/nix-homebrew'",
                "github.repository == 'zhaofengli/nix-homebrew'",
            ),
            ("github.ref == 'refs/heads/main'", "github.ref == 'refs/heads/topic'"),
            (
                "github.event.repository.default_branch == 'main'",
                "github.event.repository.default_branch == 'develop'",
            ),
            ("github.ref_protected == true", "github.ref_protected == false"),
            ("github.event_name == 'push'", "github.event_name == 'issues'"),
        )
        for expected, replacement in mutations:
            with self.subTest(replacement=replacement):
                candidate = workflow.replace(expected, replacement)
                self.assertNotEqual(candidate, workflow)
                with self.assertRaisesRegex(check.ContractError, "exact repository"):
                    check.verify_integration_workflow(candidate)

    def test_terminal_gate_cannot_skip_or_ignore_failed_evidence(self) -> None:
        workflow = self.integration_workflow()
        mutations = (
            workflow.replace("    if: always()\n", "    if: false\n", 1),
            workflow.replace(
                '          test "$CONTRACT_RESULT" = success\n',
                '          test "$CONTRACT_RESULT" = success || true\n',
                1,
            ),
            workflow.replace(
                "    timeout-minutes: 5\n",
                "    continue-on-error: true\n    timeout-minutes: 5\n",
                1,
            ),
            workflow.replace(
                "      - darwin-native-contract\n",
                "",
                1,
            ),
        )
        for candidate in mutations:
            with self.subTest(candidate=hashlib.sha256(candidate.encode()).hexdigest()):
                self.assertNotEqual(candidate, workflow)
                with self.assertRaises(check.ContractError):
                    check.verify_integration_workflow(candidate)

    def test_unapproved_trigger_is_refused(self) -> None:
        workflow = self.integration_workflow()
        candidate = workflow.replace(
            "  workflow_dispatch:\n",
            "  repository_dispatch:\n",
            1,
        )
        with self.assertRaisesRegex(check.ContractError, "trigger contract"):
            check.verify_integration_workflow(candidate)

    def test_forbidden_capability_mutations_are_refused(self) -> None:
        workflow = self.integration_workflow()
        mutations = {
            "private runner": workflow.replace(
                "    runs-on: ubuntu-24.04",
                "    runs-on: self-hosted",
                1,
            ),
            "runner group": workflow.replace(
                "    runs-on: ubuntu-24.04",
                "    runs-on:\n      group: private-fleet\n      labels: macOS",
                1,
            ),
            "write permission": workflow.replace(
                "permissions: {}",
                "permissions:\n  contents: write",
                1,
            ),
            "secret": workflow.replace(
                "          SOURCE_SHA: ${{ github.sha }}",
                "          DEPLOY_TOKEN: ${{ secrets.DEPLOY_TOKEN }}\n"
                "          SOURCE_SHA: ${{ github.sha }}",
                1,
            ),
            "credential": workflow.replace(
                "          SOURCE_SHA: ${{ github.sha }}",
                "          API_TOKEN: ${{ github.token }}\n"
                "          SOURCE_SHA: ${{ github.sha }}",
                1,
            ),
            "upstream write": workflow.replace(
                "          git init .",
                "          git push upstream HEAD:main\n          git init .",
                1,
            ),
            "release": workflow.replace(
                "          git init .",
                "          gh release create integration-test\n          git init .",
                1,
            ),
            "deployment": workflow.replace(
                "    runs-on: ubuntu-24.04",
                "    environment: production\n    runs-on: ubuntu-24.04",
                1,
            ),
        }
        for capability, candidate in mutations.items():
            with self.subTest(capability=capability):
                self.assertNotEqual(candidate, workflow)
                with self.assertRaises(check.ContractError):
                    check.verify_integration_workflow(candidate)

    def test_floating_action_is_refused(self) -> None:
        with self.assertRaisesRegex(check.ContractError, "full commit SHA"):
            check.validate_action_references(
                "steps:\n  - uses: actions/checkout@v7\n",
                Path("floating.yml"),
                self.pins["actions"],
            )

    def test_unreviewed_action_commit_is_refused(self) -> None:
        with self.assertRaisesRegex(check.ContractError, "unreviewed action revision"):
            check.validate_action_references(
                "steps:\n  - uses: actions/checkout@" + "0" * 40 + "\n",
                Path("unreviewed.yml"),
                self.pins["actions"],
            )

    def test_reviewed_action_commit_is_accepted(self) -> None:
        revision = self.pins["actions"]["actions/checkout"]["revision"]
        self.assertEqual(
            check.validate_action_references(
                f"steps:\n  - uses: actions/checkout@{revision}\n",
                Path("reviewed.yml"),
                self.pins["actions"],
            ),
            {"actions/checkout"},
        )

    def test_archived_action_tag_inventory_is_exact(self) -> None:
        archive = (
            check.ROOT / self.pins["workflowIsolation"]["archives"][0]["archivePath"]
        )
        text = archive.read_text(encoding="utf-8")
        self.assertEqual(
            check.validate_archived_action_references(
                text, archive.relative_to(check.ROOT), self.pins["actions"]
            ),
            check.EXPECTED_ARCHIVED_ACTION_REFS,
        )
        with self.assertRaisesRegex(check.ContractError, "not reviewed"):
            check.validate_archived_action_references(
                text.replace("@v7.0.1", "@v7", 1),
                archive.relative_to(check.ROOT),
                self.pins["actions"],
            )

    def test_lightweight_and_annotated_tags_resolve_to_commit(self) -> None:
        lightweight = mock.Mock(stdout="a" * 40 + "\trefs/tags/v1\n")
        annotated = mock.Mock(
            stdout=("b" * 40 + "\trefs/tags/v1\n" + "c" * 40 + "\trefs/tags/v1^{}\n")
        )
        with mock.patch.object(check, "run", return_value=lightweight):
            self.assertEqual(
                check.ls_remote_tag("https://example.invalid/x", "v1"), "a" * 40
            )
        with mock.patch.object(check, "run", return_value=annotated):
            self.assertEqual(
                check.ls_remote_tag("https://example.invalid/x", "v1"), "c" * 40
            )

    def test_duplicate_json_and_symlinked_policy_are_refused(self) -> None:
        with self.assertRaisesRegex(check.ContractError, "duplicate key"):
            check.parse_json_bytes(b'{"revision": 1, "revision": 2}', "fixture")
        with tempfile.TemporaryDirectory(prefix="axiom-json-policy-") as root:
            directory = Path(root)
            target = directory / "target.json"
            target.write_text('{"revision": 1}\n', encoding="utf-8")
            link = directory / "link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(check.ContractError, "must not be a symlink"):
                check.load_json_file(link, "fixture")

    def test_lock_transport_shaping_fields_are_refused(self) -> None:
        pin = self.pins["integrationInputs"]["nixpkgs"]
        node = {
            "locked": {
                "lastModified": pin["lastModified"],
                "narHash": pin["narHash"],
                "owner": pin["owner"],
                "repo": pin["repo"],
                "rev": pin["revision"],
                "type": "github",
            },
            "original": {
                "owner": pin["owner"],
                "repo": pin["repo"],
                "rev": pin["revision"],
                "type": "github",
            },
        }
        check.verify_github_lock_node(
            node, pin, "fixture", require_original_revision=True
        )
        mutations = []
        for section in ("locked", "original"):
            candidate = copy.deepcopy(node)
            candidate[section]["host"] = "attacker.invalid"
            mutations.append(candidate)
        candidate = copy.deepcopy(node)
        candidate["locked"]["type"] = "git"
        mutations.append(candidate)
        candidate = copy.deepcopy(node)
        candidate["original"]["rev"] = "0" * 40
        mutations.append(candidate)
        candidate = copy.deepcopy(node)
        candidate["locked"]["lastModified"] += 1
        mutations.append(candidate)
        for candidate in mutations:
            with self.subTest(candidate=candidate):
                with self.assertRaises(check.ContractError):
                    check.verify_github_lock_node(
                        candidate,
                        pin,
                        "fixture",
                        require_original_revision=True,
                    )

    def test_nix_gate_requires_sandbox_and_empty_environment(self) -> None:
        source = check.INTEGRATION / "run-nix-homebrew-gate.sh"
        text = source.read_text(encoding="utf-8")
        for fragment in (
            "env -i",
            "--option allow-import-from-derivation false",
            "--option sandbox true",
            "--option sandbox-fallback false",
            'scratch_parent=$(CDPATH= cd -P -- "$scratch_parent_input" && pwd -P)',
            'scratch=$(CDPATH= cd -P -- "$scratch" && pwd -P)',
            'if [ "$scratch_observed_parent" != "$scratch_parent" ]; then',
            'if [ ! -d "$scratch" ] || [ -L "$scratch" ]; then',
        ):
            candidate = text.replace(fragment, "removed-isolation-control")
            with tempfile.TemporaryDirectory(prefix="axiom-gate-test-") as root:
                gate = Path(root) / "run-nix-homebrew-gate.sh"
                gate.write_text(candidate, encoding="utf-8")
                with mock.patch.object(check, "INTEGRATION", Path(root)):
                    with mock.patch.object(check, "verify_nix_bootstrap"):
                        with self.assertRaisesRegex(
                            check.ContractError, "lost required isolation"
                        ):
                            check.verify_integration_files()

    def test_prohibited_runtime_registry_is_refused(self) -> None:
        forbidden = "jsr" + ".io"
        with self.assertRaisesRegex(check.ContractError, "prohibited registry"):
            check.verify_runtime_dependency_text(
                f"import module from https://{forbidden}/package",
                Path("fixture.ts"),
            )

    def test_integration_cannot_delegate_nix_bootstrap(self) -> None:
        workflow = self.integration_workflow()
        with self.assertRaises(check.ContractError):
            check.verify_integration_workflow(
                workflow + "\n# cachix/install-nix-action@" + "0" * 40 + "\n"
            )

    def test_integration_cannot_restore_github_access_token(self) -> None:
        workflow = self.integration_workflow()
        with self.assertRaises(check.ContractError):
            check.verify_integration_workflow(
                workflow + "\n# github_access_token: github.token\n"
            )

    def test_nix_bootstrap_digest_drift_is_refused(self) -> None:
        source = check.INTEGRATION / "install-nix-ci.sh"
        with tempfile.TemporaryDirectory(prefix="axiom-nix-bootstrap-test-") as root:
            candidate = Path(root) / "install-nix-ci.sh"
            candidate.write_bytes(source.read_bytes() + b"\n# drift\n")
            with self.assertRaisesRegex(check.ContractError, "digest changed"):
                check.verify_nix_bootstrap(candidate, self.pins["nix"])

    def test_nix_bootstrap_cannot_reference_live_credentials(self) -> None:
        source = check.INTEGRATION / "install-nix-ci.sh"
        with tempfile.TemporaryDirectory(prefix="axiom-nix-bootstrap-test-") as root:
            candidate = Path(root) / "install-nix-ci.sh"
            candidate.write_bytes(source.read_bytes() + b"\n# GITHUB_TOKEN\n")
            pin = dict(self.pins["nix"])
            pin["wrapperSha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
            with self.assertRaisesRegex(check.ContractError, "credential surface"):
                check.verify_nix_bootstrap(candidate, pin)

    def test_nix_bootstrap_requires_environment_and_physical_path_boundaries(
        self,
    ) -> None:
        source = check.INTEGRATION / "install-nix-ci.sh"
        text = source.read_text(encoding="utf-8")
        for fragment in (
            "env -i",
            'temporary_parent=$(CDPATH= cd -P -- "$temporary_parent_input" && pwd -P)',
            'temporary_directory=$(CDPATH= cd -P -- "$temporary_directory" && pwd -P)',
            'if [ "$temporary_observed_parent" != "$temporary_parent" ]; then',
            'if [ ! -d "$temporary_directory" ] || [ -L "$temporary_directory" ]; then',
        ):
            with self.subTest(fragment=fragment):
                with tempfile.TemporaryDirectory(
                    prefix="axiom-nix-bootstrap-test-"
                ) as root:
                    candidate = Path(root) / "install-nix-ci.sh"
                    candidate.write_text(
                        text.replace(fragment, "removed-boundary", 1),
                        encoding="utf-8",
                    )
                    pin = dict(self.pins["nix"])
                    pin["wrapperSha256"] = hashlib.sha256(
                        candidate.read_bytes()
                    ).hexdigest()
                    with self.assertRaisesRegex(
                        check.ContractError, "lost required boundary"
                    ):
                        check.verify_nix_bootstrap(candidate, pin)

    def test_legacy_promoted_tree_is_quarantined(self) -> None:
        locked = {"bin/brew", "Library/Homebrew", "Library/Homebrew/brew.sh"}
        promoted = {"bin/brew", "README.md", "LICENSE.txt"}
        check.verify_brew_tree_contract(locked, promoted, self.pins)

    def test_unrecorded_promoted_tree_recovery_is_refused(self) -> None:
        locked = {"bin/brew", "Library/Homebrew"}
        promoted = {"bin/brew", "Library/Homebrew"}
        with self.assertRaisesRegex(check.ContractError, "quarantine record"):
            check.verify_brew_tree_contract(locked, promoted, self.pins)

    def test_locked_source_materializes_exactly(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="axiom-nix-homebrew-test-"
        ) as temporary:
            destination = Path(temporary) / "source"
            revision = check.safe_extract_git_archive(
                self.pins["sourceCommit"], destination
            )
            self.assertEqual(revision, self.pins["sourceCommit"])
            self.assertTrue((destination / "modules" / "default.nix").is_file())


if __name__ == "__main__":
    unittest.main()
