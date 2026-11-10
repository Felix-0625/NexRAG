"""Describe optional search settings without probing or sending queries."""

import ipaddress
from urllib.parse import urlsplit

from src.config import settings


def _host(value):
    value = (value or "").lower()
    if value == "localhost":
        return "loopback"
    try:
        address = ipaddress.ip_address(value)
        return "loopback" if address.is_loopback or address.is_unspecified else value
    except ValueError:
        return value


def search_configuration():
    """Treat a valid configured endpoint as configuration, not proof of health.

    Never echo malformed URLs that may contain credentials. Reject WebUI-origin aliases
    before any query can reach them.
    """
    if not settings.ENABLE_WEB_SEARCH:
        return {
            "status": "disabled",
            "message": "联网搜索已停用，知识库检索和业务数据库查询仍可使用。",
        }
    endpoint = settings.SEARXNG_URL.strip()
    if not endpoint:
        return {
            "status": "unconfigured",
            "message": "尚未接入联网搜索服务，本次不会发送搜索内容。请联系管理员配置。",
        }
    try:
        parsed = urlsplit(endpoint)
        bridge = urlsplit(settings.WEBUI_TOOL_BRIDGE_URL)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        bridge_port = bridge.port or (443 if bridge.scheme == "https" else 80)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(ord(c) < 32 for c in endpoint)
        ):
            raise ValueError("invalid endpoint")
        if (
            _host(parsed.hostname) == _host(bridge.hostname)
            and port == bridge_port
            and parsed.path.rstrip("/") == bridge.path.rstrip("/")
        ):
            return {
                "status": "misconfigured",
                "message": "联网搜索地址指向了本系统的网页后端，尚未接入独立搜索服务。本次不会发送搜索内容。",
            }
    except ValueError:
        return {
            "status": "misconfigured",
            "message": "联网搜索地址无效，请管理员配置不含凭据或查询参数的 HTTP(S) 服务地址。",
        }
    return {
        "status": "configured",
        "message": "已配置搜索服务，实际可用性以查询结果为准；仅在账号与数据授权均允许时发送公开查询。",
        "endpoint": endpoint.rstrip("/"),
    }


def public_search_configuration():
    """Return public search settings without the endpoint address."""
    return {k: v for k, v in search_configuration().items() if k != "endpoint"}
