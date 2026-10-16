from app.schemas import ErrorResponse

# Shown in /docs for every route that can fail with the standard error envelope.
ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Resource not found"},
    409: {"model": ErrorResponse, "description": "Business conflict (see error.code)"},
    422: {"model": ErrorResponse, "description": "Validation error"},
}
