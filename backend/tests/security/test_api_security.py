"""HTTP surface security.

Exercises the API through a real client so that middleware ordering, header
emission and error shaping are tested as they actually run, not as they are
configured.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core.config import Settings, get_settings


@pytest.fixture
def secured_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """An app with authentication genuinely switched on."""
    secured = Settings(
        environment="test",
        api_key="viewer-key-for-tests",
        operator_api_key="operator-key-for-tests",
        allow_insecure_local=False,
        database_url="postgresql+psycopg://beroapp:beroapp@127.0.0.1:5432/beroapp_test",
    )
    get_settings.cache_clear()
    monkeypatch.setattr("app.core.config.get_settings", lambda: secured)
    monkeypatch.setattr("app.api.security.get_settings", lambda: secured)
    monkeypatch.setattr("app.api.main.get_settings", lambda: secured)
    client = TestClient(create_app(), raise_server_exceptions=False)
    yield client
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Authentication and authorisation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["/api/dashboard", "/api/markets", "/api/opportunities", "/api/audit", "/api/system"],
)
def test_unauthenticated_request_is_rejected(secured_client: TestClient, path: str) -> None:
    assert secured_client.get(path).status_code == 401


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "wrong-key",
        "viewer-key-for-test",       # one character short
        "viewer-key-for-testss",     # one character long
        "VIEWER-KEY-FOR-TESTS",      # case differs
        "viewer-key-for-tests\x00",  # null byte appended
    ],
)
def test_invalid_api_key_is_rejected(secured_client: TestClient, bad_key: str) -> None:
    response = secured_client.get("/api/markets", headers={"X-API-Key": bad_key})
    assert response.status_code == 401


def test_viewer_key_cannot_reach_an_operator_route(secured_client: TestClient) -> None:
    response = secured_client.post(
        "/api/system/kill-switch/global",
        headers={"X-API-Key": "viewer-key-for-tests"},
        json={"tripped": True, "reason": "test"},
    )
    assert response.status_code == 403


def test_health_endpoints_need_no_credential(secured_client: TestClient) -> None:
    """Liveness must work for a supervisor that holds no key."""
    assert secured_client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Headers, CORS, error shape
# ---------------------------------------------------------------------------
def test_security_headers_are_present(secured_client: TestClient) -> None:
    headers = secured_client.get("/health").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert "X-Correlation-ID" in headers


def test_security_headers_are_present_on_error_responses(secured_client: TestClient) -> None:
    """Headers must survive the middleware short-circuiting on a 401."""
    headers = secured_client.get("/api/markets").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"


def test_cors_does_not_allow_an_arbitrary_origin(secured_client: TestClient) -> None:
    response = secured_client.get(
        "/health", headers={"Origin": "https://evil.example"}
    )
    assert response.headers.get("access-control-allow-origin") != "https://evil.example"
    assert response.headers.get("access-control-allow-origin") != "*"


def test_correlation_id_is_not_reflected_unsanitised(secured_client: TestClient) -> None:
    """A caller-supplied correlation id must not become a log or header
    injection vector."""
    injected = "abc\r\nX-Injected: yes"
    response = secured_client.get("/health", headers={"X-Correlation-ID": injected})
    assert "X-Injected" not in response.headers
    assert response.headers["X-Correlation-ID"] != injected


def test_docs_are_disabled_when_not_debugging(secured_client: TestClient) -> None:
    assert secured_client.get("/docs").status_code == 404
    assert secured_client.get("/openapi.json").status_code == 404


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/api/opportunities", "?limit=99999999"),
        ("/api/opportunities", "?limit=-1"),
        ("/api/opportunities", "?limit=abc"),
        ("/api/opportunities", "?min_edge=2.5"),
        ("/api/opportunities", "?min_edge=-1"),
        ("/api/opportunities", "?min_confidence=nan"),
        ("/api/opportunities", "?max_hours_to_resolution=-1"),
        ("/api/markets", "?offset=-5"),
        ("/api/markets", "?offset=999999999999999999999"),
        ("/api/markets", "?limit=100000"),
        ("/api/audit", "?limit=0"),
        ("/api/predictions", "?limit=99999"),
    ],
)
def test_out_of_range_query_parameters_are_rejected(
    secured_client: TestClient, path: str, query: str
) -> None:
    """Bounds are declared on the parameter, so an out-of-range value is
    refused before any query is built."""
    response = secured_client.get(
        f"{path}{query}", headers={"X-API-Key": "viewer-key-for-tests"}
    )
    assert response.status_code in (422, 400), f"{path}{query} was accepted"


@pytest.mark.parametrize(
    "payload",
    [
        "'; DROP TABLE markets; --",
        "1 OR 1=1",
        "%' OR '1'='1",
        "../../../../etc/passwd",
        "<script>alert(1)</script>",
        "\x00\x01\x02",
    ],
)
def test_hostile_search_input_is_handled_safely(secured_client: TestClient, payload: str) -> None:
    """Parameter-bound queries must neither error nor execute."""
    response = secured_client.get(
        "/api/markets",
        params={"search": payload},
        headers={"X-API-Key": "viewer-key-for-tests"},
    )
    assert response.status_code in (200, 422)
    if response.status_code == 200:
        # Whatever came back is a normal, well-formed result set.
        assert "items" in response.json()


def test_oversized_body_is_rejected_before_parsing(secured_client: TestClient) -> None:
    huge = "x" * (200 * 1024)
    response = secured_client.post(
        "/api/system/kill-switch/global",
        headers={"X-API-Key": "operator-key-for-tests", "Content-Type": "application/json"},
        content=f'{{"tripped": true, "reason": "{huge}"}}',
    )
    assert response.status_code == 413


def test_path_traversal_in_a_path_parameter_does_not_resolve(secured_client: TestClient) -> None:
    response = secured_client.get(
        "/api/markets/..%2f..%2fetc%2fpasswd", headers={"X-API-Key": "viewer-key-for-tests"}
    )
    assert response.status_code in (404, 422)


def test_error_response_reveals_no_internals(secured_client: TestClient) -> None:
    response = secured_client.get(
        "/api/markets/999999999", headers={"X-API-Key": "viewer-key-for-tests"}
    )
    body = response.text.lower()
    for leak in ("traceback", "sqlalchemy", "psycopg", "file \"/", "line ", "postgresql://"):
        assert leak not in body, f"error response leaked {leak!r}"


def test_no_route_exists_for_order_placement(secured_client: TestClient) -> None:
    """Probe the surface directly: nothing that could trade may respond."""
    for path in ("/api/orders", "/api/execute", "/api/trade", "/api/withdraw", "/api/live-trading"):
        response = secured_client.post(
            path,
            headers={"X-API-Key": "operator-key-for-tests"},
            json={"size": 100},
        )
        assert response.status_code in (404, 405), f"{path} unexpectedly exists"
