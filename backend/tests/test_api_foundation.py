from uuid import UUID
import pytest
from fastapi.testclient import TestClient
from app.main import app
import app.main as main


def test_liveness_when_telemetry_initialization_fails(monkeypatch):
    calls = []

    def unavailable(*, service):
        calls.append(service)
        return False

    def shutdown():
        calls.append("shutdown")

    monkeypatch.setattr(main, "initialize", unavailable)
    monkeypatch.setattr(main, "shutdown", shutdown)

    with TestClient(app) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}
        assert calls == ["unshaken-backend"]

    assert calls == ["unshaken-backend", "shutdown"]


def assert_response_headers(response):
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"

    request_id = response.headers["x-request-id"]
    assert str(UUID(request_id)) == request_id
    return request_id


def assert_error(response, status_code, code):
    assert response.status_code == status_code
    request_id = assert_response_headers(response)
    error = response.json()["error"]
    assert set(error) == {"code", "message", "request_id"}
    assert error["code"] == code
    assert error["message"]
    assert error["request_id"] == request_id


def test_liveness_generates_its_own_request_id():
    client = TestClient(app)
    first = client.get("/health/live")
    second = client.get(
        "/health/live", headers={"X-Request-ID": "caller-supplied"}
    )

    for response in (first, second):
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    assert assert_response_headers(first) != assert_response_headers(second)


@pytest.mark.parametrize(
    ("method", "path", "body", "status_code", "code"),
    [
        ("GET", "/health/live?extra=sample-input", None, 422, "invalid_request"),
        ("GET", "/health/live", b"sample-input", 422, "invalid_request"),
        ("GET", "/missing", None, 404, "not_found"),
        ("POST", "/health/live", None, 405, "method_not_allowed"),
    ],
)


def test_client_errors(method, path, body, status_code, code):
    client = TestClient(app)
    response = client.request(method, path, content=body)

    assert_error(response, status_code, code)
    assert "sample-input" not in response.text
    if status_code == 405:
        assert response.headers["allow"] == "GET"


def test_unexpected_error_returns_safe_json(monkeypatch):
    # Restore the original routes after this test.
    monkeypatch.setattr(app.router, "routes", list(app.router.routes))

    @app.get("/_test/failure", include_in_schema=False)
    async def fail():
        raise RuntimeError("synthetic private exception detail")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/_test/failure")

    assert_error(response, 500, "internal_error")
    assert "synthetic private exception detail" not in response.text
    assert "Traceback" not in response.text


def test_openapi_documents_response_contract():
    schema = app.openapi()
    responses = schema["paths"]["/health/live"]["get"]["responses"]

    for status in ("200", "422", "404", "405", "500"):
        response = responses[status]
        assert "application/json" in response["content"]
        headers = response["headers"]
        assert headers["X-Request-ID"]["schema"]["format"] == "uuid"
        assert headers["Cache-Control"]["schema"]["enum"] == ["no-store"]

    success_schema = responses["200"]["content"]["application/json"]["schema"]
    assert success_schema["$ref"].endswith("/LivenessResponse")

    for status in ("422", "404", "405", "500"):
        error_schema = responses[status]["content"]["application/json"]["schema"]
        assert error_schema["$ref"].endswith("/ErrorResponse")

    assert "Allow" in responses["405"]["headers"]
    assert schema["components"]["schemas"]["LivenessResponse"][
        "properties"
    ]["status"]["const"] == "alive"