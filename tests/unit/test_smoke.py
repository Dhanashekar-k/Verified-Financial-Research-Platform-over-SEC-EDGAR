"""M0 smoke test: the package imports and pytest collects at least one test."""


def test_repo_imports() -> None:
    import app  # noqa: F401
    import app.engine  # noqa: F401

    assert True
