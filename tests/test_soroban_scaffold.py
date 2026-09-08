"""Tests for the Soroban contract scaffold generator (#179).

Covers the generated file structure, name normalization, detection as
``likely`` Soroban, clean import into a workspace, and the route data contract.
"""

import pytest

from app.extensions import db
from app.models import Project, Workspace
from app.services.importing import store_project_files
from app.services.soroban_scaffold import (
    DEFAULT_CRATE_NAME,
    ScaffoldError,
    normalize_crate_name,
    soroban_scaffold_rows,
)
from app.services.stellar_detection import project_stellar_metadata


@pytest.fixture()
def workspace(app, make_user, login):
    user = make_user()
    login()
    ws = Workspace(user_id=user.id, name="Scaffold workspace")
    db.session.add(ws)
    db.session.commit()
    return ws


def _by_path(rows):
    return {row["path"]: row for row in rows}


class TestScaffoldStructure:
    def test_generated_paths(self):
        rows = soroban_scaffold_rows()
        paths = {row["path"] for row in rows}
        assert paths == {
            "Cargo.toml",
            "src/lib.rs",
            ".soroban/config.toml",
            "README.md",
            ".gitignore",
        }

    def test_rows_are_sanitized_plain_text(self):
        for row in soroban_scaffold_rows():
            assert row["is_binary"] is False
            assert row["size"] == len(row["content"].encode("utf-8"))
            assert row["content"]

    def test_cargo_toml_has_soroban_sdk_and_package(self):
        cargo = _by_path(soroban_scaffold_rows())["Cargo.toml"]["content"]
        assert 'name = "hello_world"' in cargo
        assert 'edition = "2021"' in cargo
        assert "soroban-sdk" in cargo

    def test_custom_package_name_used(self):
        rows = _by_path(soroban_scaffold_rows("My Token"))
        cargo = rows["Cargo.toml"]["content"]
        assert 'name = "my_token"' in cargo
        lib = rows["src/lib.rs"]["content"]
        assert "pub struct MyToken;" in lib

    def test_lib_rs_has_contract_attributes(self):
        lib = _by_path(soroban_scaffold_rows())["src/lib.rs"]["content"]
        assert "#![no_std]" in lib
        assert "#[contract]" in lib
        assert "#[contractimpl]" in lib
        assert "use soroban_sdk::" in lib

    def test_soroban_config_targets_testnet(self):
        config = _by_path(soroban_scaffold_rows())[".soroban/config.toml"]["content"]
        assert "soroban-testnet.stellar.org" in config
        assert "Test SDF Network ; September 2015" in config

    def test_readme_does_not_claim_compilation(self):
        readme = _by_path(soroban_scaffold_rows())["README.md"]["content"]
        assert "not" in readme
        assert "compiled" in readme
        assert "no secrets" in readme.lower() or "secrets" in readme.lower()


class TestNameNormalization:
    def test_empty_falls_back_to_default(self):
        assert normalize_crate_name(None) == DEFAULT_CRATE_NAME
        assert normalize_crate_name("") == DEFAULT_CRATE_NAME
        assert normalize_crate_name("   ") == DEFAULT_CRATE_NAME

    def test_display_name_normalized(self):
        assert normalize_crate_name("My Awesome Contract") == "my_awesome_contract"
        assert normalize_crate_name("  hello-world  ") == "hello_world"
        assert normalize_crate_name("TokenVault") == "tokenvault"

    def test_leading_digit_prefixed(self):
        assert normalize_crate_name("3d-engine") == "c_3d_engine"

    def test_overlong_name_rejected(self):
        with pytest.raises(ScaffoldError):
            normalize_crate_name("a" * 60)


class TestDetection:
    def test_scaffold_detects_as_likely_soroban(self, app, db, make_user):
        owner = make_user()
        ws = Workspace(user_id=owner.id, name="Detection workspace")
        db.session.add(ws)
        db.session.commit()
        project = Project(workspace_id=ws.id, user_id=owner.id, name="scaffold", source="scaffold")
        db.session.add(project)
        db.session.commit()
        store_project_files(project, soroban_scaffold_rows("hello_world"))

        meta = project_stellar_metadata(project)
        assert meta["is_stellar"] is True
        assert meta["is_soroban"] is True
        assert meta["confidence"] == "likely"
        assert "Cargo.toml" in meta["relevant_files"]
        assert "src/lib.rs" in meta["relevant_files"]


class TestScaffoldRoute:
    def _import_url(self, workspace_id):
        return f"/workspaces/api/workspaces/{workspace_id}/projects"

    def test_scaffold_import_creates_ready_project(self, client, workspace):
        response = client.post(
            self._import_url(workspace.id),
            json={"source": "scaffold", "name": "My Token"},
        )
        assert response.status_code == 201
        data = response.get_json()
        assert data["source"] == "scaffold"
        assert data["status"] == "ready"
        assert data["name"] == "my_token"
        assert data["file_count"] == 5
        assert data["stellar"]["confidence"] == "likely"
        assert data["stellar"]["is_soroban"] is True

        project_id = data["id"]
        tree = client.get(f"/workspaces/api/projects/{project_id}/tree?path=src").get_json()
        assert [f["path"] for f in tree["files"]] == ["src/lib.rs"]

        file = client.get(f"/workspaces/api/projects/{project_id}/file?path=src/lib.rs")
        assert file.status_code == 200
        assert "#[contractimpl]" in file.get_json()["content"]

    def test_scaffold_import_listed_in_workspace(self, client, workspace):
        client.post(
            self._import_url(workspace.id),
            json={"source": "scaffold"},
        )
        projects = client.get(self._import_url(workspace.id)).get_json()
        assert any(p["source"] == "scaffold" for p in projects)

    def test_scaffold_import_requires_membership(self, app, client, make_user, login, db):
        other = make_user(username="outsider", email="outsider@example.com")
        ws = Workspace(user_id=other.id, name="Other workspace")
        db.session.add(ws)
        db.session.commit()
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        response = client.post(self._import_url(ws.id), json={"source": "scaffold", "name": "x"})
        assert response.status_code == 404
