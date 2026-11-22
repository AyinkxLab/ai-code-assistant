"""Dependency policy and SBOM tests (#623)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / "scripts" / "dependency_policy.py"
_spec = importlib.util.spec_from_file_location("dependency_policy", _SCRIPT)
dependency_policy = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dependency_policy
_spec.loader.exec_module(dependency_policy)


class TestLicenseParsing:
    def test_common_metadata_forms_normalize_to_spdx(self):
        assert dependency_policy.normalize_license("MIT") == "MIT"
        assert dependency_policy.normalize_license("MIT License") == "MIT"
        assert dependency_policy.normalize_license("BSD License") == "BSD-3-Clause"
        assert dependency_policy.normalize_license("Apache Software License") == "Apache-2.0"
        assert dependency_policy.normalize_license("Apache-2.0 OR BSD-2-Clause") == (
            "Apache-2.0 OR BSD-2-Clause"
        )

    def test_unparseable_metadata_is_unknown(self):
        assert dependency_policy.normalize_license("") == "UNKNOWN"
        assert dependency_policy.normalize_license("Proprietary vendor terms") == "UNKNOWN"

    def test_allowlist_accepts_known_and_rejects_copyleft(self):
        assert dependency_policy.license_allowed("MIT")
        assert dependency_policy.license_allowed("Apache-2.0 OR BSD-3-Clause")
        assert not dependency_policy.license_allowed("GPL-3.0-only")
        assert not dependency_policy.license_allowed("UNKNOWN")


class TestRequirementParsing:
    def test_declared_requirements_and_includes_are_collected(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "# comment\nFlask==3.1.3\nrequests==2.33.0\n", encoding="utf-8"
        )
        (tmp_path / "requirements-dev.txt").write_text(
            "-r requirements.txt\npytest==9.0.3\n", encoding="utf-8"
        )

        dependencies = dependency_policy.collect_dependencies(tmp_path)

        assert [dep.name for dep in dependencies] == ["Flask", "pytest", "requests"]
        assert {dep.version for dep in dependencies} == {"3.1.3", "2.33.0", "9.0.3"}
        assert all(dep.pinned for dep in dependencies)


class TestSbomShape:
    def test_sbom_is_bounded_and_declares_its_own_schema(self):
        dependencies = [
            dependency_policy.Dependency(
                name=f"package-{index}",
                version="1.0",
                license="MIT" if index % 2 else "GPL-3.0-only",
                pinned=True,
                source="requirements.txt",
            )
            for index in range(dependency_policy.MAX_COMPONENTS + 10)
        ]

        sbom = dependency_policy.build_sbom(dependencies)

        assert sbom["schema"] == dependency_policy.SCHEMA
        assert "CycloneDX" not in sbom["schema"]
        assert sbom["format_note"].startswith("Minimal project SBOM")
        assert sbom["component_count"] == dependency_policy.MAX_COMPONENTS
        assert sbom["truncated"] is True
        assert len(sbom["components"]) == dependency_policy.MAX_COMPONENTS
        json.dumps(sbom)  # must be serializable with the stdlib only

    def test_long_fields_are_truncated(self):
        dependency = dependency_policy.Dependency(
            name="x" * 500,
            version="1.0",
            license="MIT",
            pinned=True,
            source="requirements.txt",
        )

        component = dependency_policy.build_sbom([dependency])["components"][0]

        assert len(component["name"]) == dependency_policy.MAX_FIELD_CHARS


class TestLicenseEnforcement:
    def test_disallowed_license_is_reported(self):
        dependencies = [
            dependency_policy.Dependency("safe", "1.0", "MIT", True, "requirements.txt"),
            dependency_policy.Dependency("risky", "2.0", "GPL-3.0-only", True, "requirements.txt"),
        ]

        violations = dependency_policy.find_license_violations(dependencies)

        assert violations == ["risky 2.0: disallowed license 'GPL-3.0-only'"]

    def test_main_fails_with_offenders(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "requirements.txt").write_text("risky==2.0\n", encoding="utf-8")
        monkeypatch.setattr(dependency_policy, "license_for", lambda _name: "GPL-3.0-only")

        assert dependency_policy.main(["--root", str(tmp_path)]) == 1
        output = capsys.readouterr().out
        assert "Dependency license policy failed" in output
        assert "risky 2.0" in output

    def test_main_writes_a_sbom_when_clean(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "requirements.txt").write_text("safe==1.0\n", encoding="utf-8")
        monkeypatch.setattr(dependency_policy, "license_for", lambda _name: "MIT")
        output_path = tmp_path / "sbom.json"

        assert dependency_policy.main(["--root", str(tmp_path), "--sbom", str(output_path)]) == 0

        sbom = json.loads(output_path.read_text(encoding="utf-8"))
        assert sbom["components"][0]["name"] == "safe"
        assert "Checked 1 declared dependencies" in capsys.readouterr().out
