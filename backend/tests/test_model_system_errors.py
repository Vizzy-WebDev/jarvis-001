from jarvis.model_system.errors import ErrorKind, benches_the_model, classify, refused_parameter


class _Err(Exception):
    def __init__(self, message, status=None, status_code=None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if status_code is not None:
            self.status_code = status_code


def test_status_codes_map_to_kinds():
    assert classify(_Err("nope", status_code=429)) is ErrorKind.RATE_LIMIT
    assert classify(_Err("nope", status_code=401)) is ErrorKind.AUTHENTICATION
    assert classify(_Err("nope", status_code=403)) is ErrorKind.PERMISSION_DENIED
    assert classify(_Err("nope", status_code=404)) is ErrorKind.MODEL_UNAVAILABLE
    assert classify(_Err("nope", status_code=503)) is ErrorKind.PROVIDER_UNAVAILABLE


def test_context_length_text_is_its_own_kind_not_unsupported():
    err = _Err("This model's maximum context length is 8192 tokens.")
    assert classify(err) is ErrorKind.CONTEXT_EXCEEDED


def test_refused_parameter_requires_both_phrase_and_name():
    named = _Err("Unrecognized request argument supplied: reasoning_effort")
    assert refused_parameter(named) == "reasoning_effort"
    assert classify(named) is ErrorKind.UNSUPPORTED_PARAMETER

    unrelated = _Err("is not supported in this region")
    assert refused_parameter(unrelated) is None


def test_none_is_unknown():
    assert classify(None) is ErrorKind.UNKNOWN


def test_a_classifier_failure_never_raises():
    class Hostile:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    assert classify(Hostile()) is ErrorKind.UNKNOWN  # type: ignore[arg-type]


def test_model_level_kinds_are_the_ones_that_bench():
    assert benches_the_model(ErrorKind.AUTHENTICATION) is True
    assert benches_the_model(ErrorKind.RATE_LIMIT) is True
    assert benches_the_model(ErrorKind.CONTEXT_EXCEEDED) is False
    assert benches_the_model(ErrorKind.UNSUPPORTED_PARAMETER) is False
    assert benches_the_model(ErrorKind.INVALID_REQUEST) is False


def test_gemini_shaped_json_message_is_read():
    err = _Err('{"error": {"code": 429, "message": "Resource exhausted"}}')
    assert classify(err) is ErrorKind.RATE_LIMIT
