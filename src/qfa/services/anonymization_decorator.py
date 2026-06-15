# 1. Capture the specific subclasses of BaseModel used for Request and Response
from functools import wraps
from typing import Any, Callable, Concatenate, ParamSpec, TypeVar

from pydantic import BaseModel

from qfa.domain import AnonymizationPort

RequestT = TypeVar("RequestT", bound=BaseModel)
ResponseT = TypeVar("ResponseT", bound=BaseModel)

# 2. Capture any extra positional/keyword arguments (*args, **kwargs)
P = ParamSpec("P")


def handle_anonymization(
    method: Callable[Concatenate[Any, RequestT, P], ResponseT],
) -> Callable[Concatenate[Any, RequestT, P], ResponseT]:
    """Wrap a request/response method with anonymization and deanonymization."""

    @wraps(method)
    def wrapper(
        self: Any, request: RequestT, *args: P.args, **kwargs: P.kwargs
    ) -> ResponseT:
        anonymizer: AnonymizationPort | None = getattr(self, "anonymizer", None)
        if anonymizer is None or not isinstance(anonymizer, AnonymizationPort):
            raise ValueError(
                f"Anonymizer not found or the class {type(self).__name__} does not implement AnonymizationPort"
            )

        if not isinstance(request, BaseModel):
            raise ValueError(
                f"Expected request to be a Pydantic BaseModel, got {type(request).__name__}"
            )

        unanonymized_input_string = request.model_dump_json()
        anonymized_input_string, mapping = anonymizer.anonymize(
            unanonymized_input_string
        )

        # Using type(request) preserves the exact subclass type
        anonymized_request = type(request).model_validate_json(anonymized_input_string)

        anonymized_response = method(self, anonymized_request, *args, **kwargs)

        if not isinstance(anonymized_response, BaseModel):
            raise ValueError(
                f"Expected response to be a Pydantic BaseModel, got {type(anonymized_response).__name__}"
            )

        anonymized_response_string = anonymized_response.model_dump_json()
        deanonymized_response_string = anonymizer.deanonymize(
            anonymized_response_string, mapping
        )

        # Using type(anonymized_response) preserves the exact subclass type
        deanonymized_response = type(anonymized_response).model_validate_json(
            deanonymized_response_string
        )

        return deanonymized_response

    return wrapper
