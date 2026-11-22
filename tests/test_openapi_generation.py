"""Tests for the generated OpenAPI document (#652)."""

from scripts import generate_openapi


def test_document_is_deterministic(app):
    first = generate_openapi.render(app)
    assert first == generate_openapi.render(app)


def test_document_is_valid_and_covers_public_routes(app):
    document = generate_openapi.build_document(app)

    assert generate_openapi.validate_document(document) == []
    assert "/workspaces/api/workspaces" in document["paths"]
    assert "/workspaces/api/workspaces/{workspace_id}/members" in document["paths"]

    operation = document["paths"]["/workspaces/api/workspaces"]["get"]
    assert operation["responses"]["429"]["$ref"].endswith("RateLimited")


def test_path_parameters_become_typed_openapi_parameters(app):
    document = generate_openapi.build_document(app)
    operation = document["paths"]["/workspaces/api/workspaces/{workspace_id}/members"]["get"]

    assert operation["parameters"] == [
        {
            "name": "workspace_id",
            "in": "path",
            "required": True,
            "schema": {"type": "integer"},
        }
    ]
