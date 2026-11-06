"""一次性的原生会话测试，直接执行已发布的身份/认证函数。

不会改动任何真实的 WebUI 账号、密码或服务配置。
"""

import ast
import asyncio
import importlib.util
import logging
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import HTTPException, Response, status

BACKEND = Path("webui/backend/open_webui")


def shipped_functions(path, names, namespace):
    tree = ast.parse(path.read_text("utf8"))
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    assert {node.name for node in functions} == set(names)
    for node in functions:
        node.decorator_list = []
        node.returns = None
        node.args.defaults = [ast.Constant(None) for _ in node.args.defaults]
        node.args.kw_defaults = [
            ast.Constant(None) if value is not None else None for value in node.args.kw_defaults
        ]
        for argument in [*node.args.args, *node.args.kwonlyargs]:
            argument.annotation = None
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


@pytest.fixture
def identity(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXRAG_WORKSPACE", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "native_identity_test", BACKEND / "utils/nexrag_identity.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "open_webui.utils.nexrag_identity", module)
    disconnect = SimpleNamespace(disconnect_user_sessions=AsyncMock())
    monkeypatch.setitem(sys.modules, "open_webui.socket.main", disconnect)
    return module


@pytest.fixture
def auth(identity):
    namespace = {
        "asyncio": asyncio,
        "datetime": datetime,
        "UTC": UTC,
        "uuid": uuid,
        "jwt": jwt,
        "SESSION_SECRET": "disposable-test-session-secret-32-chars",
        "ALGORITHM": "HS256",
        "log": logging.getLogger("native-identity-test"),
        "ENABLE_PASSWORD_VALIDATION": False,
        "PASSWORD_HASH_ALGORITHM": "argon2",
        "HTTPException": HTTPException,
        "status": status,
        "ENABLE_OTEL": False,
        "WEBUI_AUTH_TRUSTED_EMAIL_HEADER": "",
        "Users": SimpleNamespace(get_user_by_id=AsyncMock(), update_last_active_by_id=AsyncMock()),
        "ERROR_MESSAGES": SimpleNamespace(INVALID_TOKEN="invalid", UNAUTHORIZED="unauthorized"),
        "PASSWORD_BCRYPT_MAX_BYTES": 72,
    }
    return shipped_functions(
        BACKEND / "utils/auth.py",
        [
            "create_token",
            "decode_token",
            "is_valid_token",
            "invalidate_token",
            "revoke_user_tokens",
            "validate_password",
            "get_current_user",
            "get_current_user_by_api_key",
        ],
        namespace,
    )


def request(path="/api/v1/chats/", method="GET", token=""):
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        method=method,
        cookies={},
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(redis=None)),
        state=SimpleNamespace(token=None),
    )


def test_reset_revokes_old_tokens_without_redis_and_forces_password_change(identity, auth):
    first = auth["decode_token"](auth["create_token"]({"id": "employee"}, timedelta(hours=1)))
    assert asyncio.run(auth["is_valid_token"](first, None))
    asyncio.run(auth["revoke_user_tokens"](request(), "employee"))
    identity.provision("employee")
    assert not asyncio.run(auth["is_valid_token"](first, None))
    assert identity.state("employee")["must_change_password"]
    current = auth["decode_token"](auth["create_token"]({"id": "employee"}, timedelta(hours=1)))
    assert asyncio.run(auth["is_valid_token"](current, None))
    assert current["nsv"] > first["nsv"]


def test_signout_revocation_does_not_revoke_other_current_session(identity, auth):
    first = auth["create_token"]({"id": "employee"}, timedelta(hours=1))
    second = auth["create_token"]({"id": "employee"}, timedelta(hours=1))
    asyncio.run(auth["invalidate_token"](request(), first))
    assert not asyncio.run(auth["is_valid_token"](auth["decode_token"](first), None))
    assert asyncio.run(auth["is_valid_token"](auth["decode_token"](second), None))


def test_initial_credentials_cannot_call_business_or_api_key_routes(identity, auth):
    identity.provision("employee")
    auth["Users"].get_user_by_id.return_value = SimpleNamespace(id="employee", role="user")
    token = auth["create_token"]({"id": "employee"}, timedelta(hours=1))

    async def run():
        with pytest.raises(HTTPException) as error:
            await auth["get_current_user"](
                request(), Response(), None, SimpleNamespace(credentials=token)
            )
        assert error.value.status_code == 403
        assert error.value.detail["code"] == "password_change_required"
        with pytest.raises(HTTPException):
            await auth["get_current_user_by_api_key"](request(), "sk-disposable")

    asyncio.run(run())


def test_forced_account_can_inspect_session_and_change_password_only(identity):
    assert identity.password_change_allowed("/api/v1/auths/", "GET")
    assert identity.password_change_allowed("/api/v1/auths/nexrag/identity", "GET")
    assert identity.password_change_allowed("/api/v1/auths/nexrag/change-password", "POST")
    assert not identity.password_change_allowed("/api/v1/auths/api_key", "POST")
    assert not identity.password_change_allowed("/api/v1/users/user/settings/update", "POST")


def test_existing_accounts_not_forced_and_legacy_token_revoked_after_change(identity):
    legacy = {"id": "existing", "iat": int(time.time()), "jti": "legacy"}
    assert not identity.state("existing")["must_change_password"]
    assert identity.accepts(legacy)
    identity.revoke_all("existing")
    assert not identity.accepts(legacy)


def test_late_password_change_cannot_clear_newer_admin_reset_requirement(identity):
    identity.provision("employee")
    generation = identity.revoke_all("employee")
    identity.provision("employee")
    assert not identity.password_changed("employee", generation)
    assert identity.state("employee")["must_change_password"]


def test_actual_password_change_clears_gate_and_rotates_token(identity, auth):
    identity.provision("employee")
    old = auth["decode_token"](auth["create_token"]({"id": "employee"}, timedelta(hours=1)))
    employee = SimpleNamespace(
        id="employee", email="employee@disposable.local", role="user", name="Disposable"
    )
    namespace = dict(auth)
    namespace.update(
        Auths=SimpleNamespace(
            authenticate_user=AsyncMock(return_value=employee),
            update_user_password_by_id=AsyncMock(return_value=True),
        ),
        get_password_hash=AsyncMock(return_value="disposable-hash"),
        publish_event=AsyncMock(),
        EVENTS=SimpleNamespace(AUTH_PASSWORD_CHANGED="changed"),
    )
    shipped_functions(BACKEND / "routers/auths.py", ["update_password"], namespace)
    form = SimpleNamespace(
        password="initial passphrase only", new_password="changed passphrase only"
    )
    assert asyncio.run(namespace["update_password"](request(), form, employee, None))
    assert not identity.state("employee")["must_change_password"]
    assert not identity.accepts(old)
    fresh = auth["decode_token"](auth["create_token"]({"id": "employee"}, timedelta(hours=1)))
    assert identity.accepts(fresh)
    with pytest.raises(HTTPException):
        asyncio.run(
            namespace["update_password"](
                request(),
                SimpleNamespace(password=form.password, new_password=form.password),
                employee,
                None,
            )
        )


def test_native_password_policy_and_signup_cannot_be_reenabled(identity, auth):
    with pytest.raises(ValueError, match="15"):
        auth["validate_password"]("short")
    assert auth["validate_password"]("a long passphrase for work")
    with pytest.raises(ValueError, match="常见"):
        auth["validate_password"]("passwordpassword")
    namespace = {
        "Users": SimpleNamespace(has_users=AsyncMock(return_value=True)),
        "HTTPException": HTTPException,
    }
    shipped_functions(BACKEND / "routers/auths.py", ["signup"], namespace)
    with pytest.raises(HTTPException) as error:
        asyncio.run(namespace["signup"](request(), Response(), SimpleNamespace(), None))
    assert error.value.status_code == 403


def test_actual_admin_add_marks_initial_password_and_actual_reset_revokes(identity, auth):
    employee = SimpleNamespace(
        id="employee-new", email="employee@disposable.local", role="user", name="Disposable"
    )
    admin = SimpleNamespace(id="admin-fixture", role="admin")
    form = SimpleNamespace(
        email=employee.email,
        name=employee.name,
        password="initial passphrase only",
        role="user",
        profile_image_url="",
    )
    namespace = dict(auth)
    namespace.update(
        Users=SimpleNamespace(
            get_user_by_email=AsyncMock(return_value=None),
            get_first_user=AsyncMock(return_value=admin),
            get_user_by_id=AsyncMock(return_value=employee),
        ),
        Auths=SimpleNamespace(
            insert_new_auth=AsyncMock(return_value=employee),
            update_user_password_by_id=AsyncMock(return_value=True),
        ),
        validate_email_format=lambda email: True,
        get_password_hash=AsyncMock(return_value="disposable-hash"),
        apply_default_group_assignment=AsyncMock(),
        publish_event=AsyncMock(),
        Config=SimpleNamespace(get=AsyncMock(return_value=None)),
        parse_duration=lambda value: timedelta(hours=1),
        EVENTS=SimpleNamespace(USER_CREATED="created", AUTH_PASSWORD_CHANGED="changed"),
    )
    shipped_functions(BACKEND / "routers/auths.py", ["add_user"], namespace)
    response = asyncio.run(namespace["add_user"](request(), form, admin, None))
    assert identity.state(employee.id)["must_change_password"]
    initial = auth["decode_token"](response["token"])
    assert identity.accepts(initial)
    update = SimpleNamespace(
        password="replacement passphrase only",
        role=None,
        email=None,
        name=None,
        profile_image_url=None,
    )
    shipped_functions(BACKEND / "routers/users.py", ["update_user_by_id"], namespace)
    assert (
        asyncio.run(namespace["update_user_by_id"](request(), employee.id, update, admin, None))
        == employee
    )
    assert not identity.accepts(initial)
    assert identity.state(employee.id)["must_change_password"]
    events = identity.security_events(target_id=employee.id)
    assert {event["action"] for event in events} == {"account.provisioned", "password.reset"}
    assert all(event["actor_id"] == admin.id for event in events)
    assert "password" not in events[0] and "token" not in events[0]


def test_change_endpoint_returns_new_session_and_keeps_old_token_invalid(identity, auth):
    employee = SimpleNamespace(
        id="employee", email="employee@disposable.local", role="user", name="Disposable"
    )
    identity.provision(employee.id)
    old = auth["decode_token"](auth["create_token"]({"id": employee.id}, timedelta(hours=1)))
    namespace = dict(auth)
    namespace.update(
        Auths=SimpleNamespace(
            authenticate_user=AsyncMock(return_value=employee),
            update_user_password_by_id=AsyncMock(return_value=True),
        ),
        get_password_hash=AsyncMock(return_value="disposable-hash"),
        publish_event=AsyncMock(),
        EVENTS=SimpleNamespace(AUTH_PASSWORD_CHANGED="changed", AUTH_LOGIN="login"),
        Config=SimpleNamespace(get=AsyncMock(return_value=None)),
        parse_duration=lambda value: timedelta(hours=1),
        get_permissions=AsyncMock(return_value={}),
        WEBUI_AUTH_COOKIE_SAME_SITE="lax",
        WEBUI_AUTH_COOKIE_SECURE=False,
        datetime=__import__("datetime"),
        time=time,
    )
    shipped_functions(
        BACKEND / "routers/auths.py",
        ["update_password", "native_change_password", "create_session_response"],
        namespace,
    )
    form = SimpleNamespace(
        password="initial passphrase only", new_password="replacement passphrase only"
    )
    response = Response()
    result = asyncio.run(
        namespace["native_change_password"](request(), response, form, employee, None)
    )
    assert result["must_change_password"] is False
    assert not identity.accepts(old)
    assert identity.accepts(auth["decode_token"](result["token"]))
    assert "httponly" in response.headers["set-cookie"].lower()
    assert {event["action"] for event in identity.security_events()} == {
        "password.changed",
        "session.signed_in",
    }


def test_forwarded_generation_is_original_verified_token_not_latest_store(identity, auth):
    employee = SimpleNamespace(
        id="employee", email="employee@disposable.local", name="Disposable", role="user"
    )
    auth["Users"].get_user_by_id.return_value = employee
    token = auth["create_token"]({"id": employee.id}, timedelta(hours=1))
    incoming = request()

    async def authenticate():
        return await auth["get_current_user"](
            incoming, Response(), None, SimpleNamespace(credentials=token)
        )

    assert asyncio.run(authenticate()) == employee
    assert incoming.state.nexrag_session_version == 0
    identity.revoke_all(employee.id)
    namespace = {
        "time": time,
        "jwt": jwt,
        "FORWARD_USER_INFO_HEADER_JWT_EXPIRES_SECONDS": 300,
        "FORWARD_USER_INFO_HEADER_JWT_SECRET": "forward-test-secret-at-least-32-characters",
        "FORWARD_USER_INFO_HEADER_JWT": "X-Test-Identity",
        "FORWARD_USER_INFO_HEADER_AUTH_TYPE": "X-Test-Auth",
    }
    shipped_functions(
        BACKEND / "utils/headers.py",
        ["_mint_forward_user_jwt", "include_user_info_headers"],
        namespace,
    )
    headers = namespace["include_user_info_headers"]({}, employee, request=incoming)
    forwarded = jwt.decode(
        headers["X-Test-Identity"],
        namespace["FORWARD_USER_INFO_HEADER_JWT_SECRET"],
        algorithms=["HS256"],
    )
    assert forwarded["session_version"] == 0
    assert identity.state(employee.id)["session_version"] == 1


def test_native_login_identity_cannot_be_changed_or_recycled_by_deletion(identity, auth):
    admin = SimpleNamespace(id="admin-fixture", role="admin")
    employee = SimpleNamespace(id="employee", email="employee@disposable.local")
    namespace = dict(auth)
    namespace.update(
        Users=SimpleNamespace(
            get_first_user=AsyncMock(return_value=admin),
            get_user_by_id=AsyncMock(return_value=employee),
        )
    )
    shipped_functions(
        BACKEND / "routers/users.py", ["update_user_by_id", "delete_user_by_id"], namespace
    )
    form = SimpleNamespace(email="replacement@disposable.local", role=None)
    with pytest.raises(HTTPException, match="不可变"):
        asyncio.run(namespace["update_user_by_id"](request(), employee.id, form, admin, None))
    with pytest.raises(HTTPException, match="停用"):
        asyncio.run(namespace["delete_user_by_id"](request(), employee.id, admin, None))


def test_security_audit_log_is_metadata_only_and_endpoint_requires_admin(identity):
    identity.record_event("admin", "employee", "password.reset")
    event = identity.security_events(target_id="employee")[0]
    assert set(event) == {"id", "actor_id", "target_id", "action", "created_at"}
    assert identity.security_events(target_id="someone-else") == []
    with pytest.raises(ValueError):
        identity.record_event("admin", "employee", "arbitrary-private-content")
    source = ast.parse((BACKEND / "routers/auths.py").read_text("utf8"))
    endpoint = next(
        node
        for node in source.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "native_security_events"
    )
    assert any(
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "Depends"
        and value.args
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id == "get_admin_user"
        for value in endpoint.args.defaults
    )
