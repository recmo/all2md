from doc2md.http import resilient_session


def test_resilient_session_retries_transient_provider_failures() -> None:
    retries = resilient_session().get_adapter("https://").max_retries

    assert retries.total == 5
    assert retries.backoff_factor == 1
    assert retries.status_forcelist == (429, 500, 502, 503, 504)
    assert retries.allowed_methods == frozenset({"GET", "POST"})
    assert retries.respect_retry_after_header
