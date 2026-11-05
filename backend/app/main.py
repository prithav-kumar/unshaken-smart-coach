from uuid import UUID, uuid4
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from contextlib import asynccontextmanager
from app.telemetry import initialize, shutdown
from typing import Literal
from pydantic import BaseModel


class LivenessResponse(BaseModel):
    status: Literal["alive"]


class ErrorDetail(BaseModel):
    code: Literal[
        "invalid_request",
        "not_found",
        "method_not_allowed",
        "internal_error",
    ]
    message: str
    request_id: UUID


class ErrorResponse(BaseModel):
    error: ErrorDetail


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize(service="unshaken-backend")
    try:
        yield
    finally:
        shutdown()


app = FastAPI(lifespan=lifespan)

ERROR_DETAILS = {
    404: ("not_found", "The requested route was not found."),
    405: ("method_not_allowed", "This method is not allowed for the route."),
    422: ("invalid_request", "The request contains unsupported input."),
    500: ("internal_error", "An unexpected error occurred."),
}


def error_response(request: Request, status_code: int, headers=None) -> JSONResponse:
    if status_code not in ERROR_DETAILS:
        status_code = 500

    code, message = ERROR_DETAILS[status_code]

    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request.state.request_id,
            }
        },
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(request: Request, exc: StarletteHTTPException):
    return error_response(request, exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, _exc: RequestValidationError):
    return error_response(request, 422)


@app.middleware("http")
async def add_response_headers(request: Request, call_next):
    request_id = str(uuid4())
    request.state.request_id = request_id

    try:
        response = await call_next(request)
    except Exception:
        response = error_response(request, 500)
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    return response


RESPONSE_HEADERS = {
    "X-Request-ID": {
        "description": "Server-generated UUID for this request.",
        "schema": {"type": "string", "format": "uuid"},
    },
    "Cache-Control": {
        "description": "Response caching policy.",
        "schema": {"type": "string", "enum": ["no-store"]},
    },
}


@app.get(
    "/health/live",
    response_model=LivenessResponse,
    responses={
        200: {
            "description": "The backend process can answer.",
            "headers": RESPONSE_HEADERS,
        },
        422: {
            "description": "Unsupported query parameter or nonempty body.",
            "model": ErrorResponse,
            "headers": RESPONSE_HEADERS,
        },
        404: {
            "description": "Unknown route.",
            "model": ErrorResponse,
            "headers": RESPONSE_HEADERS,
        },
        405: {
            "description": "Unsupported HTTP method.",
            "model": ErrorResponse,
            "headers": {
                **RESPONSE_HEADERS,
                "Allow": {
                    "description": "Supported HTTP methods.",
                    "schema": {"type": "string"},
                    "example": "GET",
                },
            },
        },
        500: {
            "description": "Unexpected application failure.",
            "model": ErrorResponse,
            "headers": RESPONSE_HEADERS,
        },
    },
)


async def liveness(request: Request):
    if request.query_params:
        return error_response(request, 422)

    async for chunk in request.stream():
        if chunk:
            return error_response(request, 422)

    return {"status": "alive"}