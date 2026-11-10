"""Native attachment authorization against isolated file and knowledge stores."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


@pytest.fixture
def attachments():
    """Provide attachments test data or behavior for this scenario."""
    path = (
        Path(__file__).resolve().parents[1] / "webui/backend/open_webui/utils/nexrag_attachments.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [
        node
        for node in tree.body
        if not isinstance(node, ast.ImportFrom) or not node.module.startswith("open_webui")
    ]
    scope = {
        "DEFAULT_POLICY": dict(
            classification="internal",
            external_models=False,
            external_search=False,
            external_tools=False,
        ),
        "require_file": AsyncMock(),
        "require_knowledge": AsyncMock(),
        "Knowledges": SimpleNamespace(get_knowledges_by_file_id=AsyncMock(return_value=[])),
    }
    exec(compile(tree, str(path), "exec"), scope)
    return scope


def test_references_use_file_ids_not_client_collection(attachments):
    ids = attachments["attachment_ids"](
        [
            {"type": "file", "id": "own-file", "collection_name": "other-private"},
            {"type": "file", "file": {"id": "own-file"}},
            {"type": "collection", "id": "already-authorized-kb"},
        ]
    )
    assert ids == ["own-file"]


@pytest.mark.parametrize(
    "item",
    [
        {"type": "file", "id": "../other"},
        {"type": "text", "file": "bad"},
        {"type": "file", "url": "https://example.com/private"},
        "bad",
    ],
)
def test_malformed_references_rejected(attachments, item):
    with pytest.raises(HTTPException):
        attachments["attachment_ids"]([item])


def test_verified_file_collection_is_local_only(attachments):
    user = SimpleNamespace(id="owner", role="user")
    labels = asyncio.run(attachments["attachment_labels"](user, ["own-file"]))
    attachments["require_file"].assert_awaited_once_with("own-file", user)
    assert labels == {"file-own-file": attachments["DEFAULT_POLICY"]}


def test_admin_cannot_attach_another_private_file(attachments):
    attachments["require_file"].side_effect = HTTPException(404, "not authorized")
    with pytest.raises(HTTPException):
        asyncio.run(attachments["attachment_labels"](SimpleNamespace(role="admin"), ["private"]))
    attachments["Knowledges"].get_knowledges_by_file_id.assert_not_called()


def test_knowledge_file_uses_current_library_not_stale_upload(attachments):
    attachments["Knowledges"].get_knowledges_by_file_id.return_value = [
        SimpleNamespace(id="current-kb")
    ]
    user = SimpleNamespace(id="employee")
    assert asyncio.run(attachments["attachment_labels"](user, ["policy-file"])) == {}
    attachments["require_knowledge"].assert_awaited_once_with("current-kb", user)
