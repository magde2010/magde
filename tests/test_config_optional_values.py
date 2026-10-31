import config


def test_optional_env_float_falls_back_for_invalid_and_non_finite_values(monkeypatch):
    monkeypatch.setenv("MAGDE_TEST_OPTIONAL_FLOAT", "not-a-number")
    assert config.optional_env_float("MAGDE_TEST_OPTIONAL_FLOAT", 5.0, 1.0) == 5.0

    monkeypatch.setenv("MAGDE_TEST_OPTIONAL_FLOAT", "NaN")
    assert config.optional_env_float("MAGDE_TEST_OPTIONAL_FLOAT", 5.0, 1.0) == 5.0

    monkeypatch.setenv("MAGDE_TEST_OPTIONAL_FLOAT", "Infinity")
    assert config.optional_env_float("MAGDE_TEST_OPTIONAL_FLOAT", 5.0, 1.0) == 5.0


def test_optional_env_float_applies_the_safe_minimum(monkeypatch):
    monkeypatch.setenv("MAGDE_TEST_OPTIONAL_FLOAT", "-2")
    assert config.optional_env_float("MAGDE_TEST_OPTIONAL_FLOAT", 5.0, 1.0) == 1.0

    monkeypatch.setenv("MAGDE_TEST_OPTIONAL_FLOAT", "7.5")
    assert config.optional_env_float("MAGDE_TEST_OPTIONAL_FLOAT", 5.0, 1.0) == 7.5


def test_env_value_removes_deployment_copy_paste_wrappers(monkeypatch):
    monkeypatch.setenv("MAGDE_TEST_SECRET", '  "key-value"  ')
    assert config.env_value("MAGDE_TEST_SECRET") == "key-value"

    monkeypatch.setenv("MAGDE_TEST_SECRET", "   'secret-value'   ")
    assert config.env_value("MAGDE_TEST_SECRET") == "secret-value"

    monkeypatch.setenv("MAGDE_TEST_SECRET", "   ")
    assert config.env_value("MAGDE_TEST_SECRET") is None
