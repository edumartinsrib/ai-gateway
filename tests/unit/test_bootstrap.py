from pathlib import Path

import pytest
import yaml

from ai_gateway.bootstrap import (
    ConfigError,
    iter_env_refs,
    load_secret_files,
    prepare,
    render_config,
    validate,
)

REPO = Path(__file__).resolve().parents[2]

BASE_ENV = {
    "AZURE_API_KEY": "azure-key",
    "AZURE_API_BASE": "https://example.openai.azure.com",
    "AZURE_API_VERSION": "2025-04-01-preview",
    "LITELLM_MASTER_KEY": "sk-master",
    "LITELLM_SALT_KEY": "sk-salt",
    "DATABASE_URL": "postgresql://u:p@db:5432/litellm",
}


def minimal_config() -> dict:
    return {
        "model_list": [
            {"model_name": "m", "litellm_params": {"model": "azure/m", "api_key": "os.environ/AZURE_API_KEY"}}
        ],
        "litellm_settings": {"callbacks": ["prometheus", "otel"]},
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
    }


def test_secret_file_is_loaded_when_env_is_missing(tmp_path):
    secret = tmp_path / "azure"
    secret.write_text("from-vault\n")
    env = {"AZURE_API_KEY_FILE": str(secret)}
    assert load_secret_files(env) == ["AZURE_API_KEY"]
    assert env["AZURE_API_KEY"] == "from-vault"


def test_env_var_wins_over_secret_file(tmp_path):
    secret = tmp_path / "azure"
    secret.write_text("from-file")
    env = {"AZURE_API_KEY": "from-env", "AZURE_API_KEY_FILE": str(secret)}
    assert load_secret_files(env) == []
    assert env["AZURE_API_KEY"] == "from-env"


@pytest.mark.parametrize("content", [None, ""])
def test_unreadable_or_empty_secret_file_fails(tmp_path, content):
    secret = tmp_path / "azure"
    if content is not None:
        secret.write_text(content)
    with pytest.raises(ConfigError, match="AZURE_API_KEY_FILE"):
        load_secret_files({"AZURE_API_KEY_FILE": str(secret)})


def test_iter_env_refs_finds_nested_references():
    assert set(iter_env_refs(minimal_config())) == {"AZURE_API_KEY", "LITELLM_MASTER_KEY"}


def test_otel_callback_is_dropped_without_endpoint():
    rendered = render_config(minimal_config(), {})
    assert rendered["litellm_settings"]["callbacks"] == ["prometheus"]


def test_otel_callback_is_kept_with_endpoint():
    rendered = render_config(minimal_config(), {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4317"})
    assert "otel" in rendered["litellm_settings"]["callbacks"]


def test_redis_is_injected_when_redis_host_is_set():
    env = {"REDIS_HOST": "redis", "REDIS_PASSWORD": "pw"}
    rendered = render_config(minimal_config(), env)
    assert rendered["router_settings"] == {
        "redis_host": "os.environ/REDIS_HOST",
        "redis_port": "os.environ/REDIS_PORT",
        "redis_password": "os.environ/REDIS_PASSWORD",
    }
    assert rendered["general_settings"]["use_redis_transaction_buffer"] is True
    assert env["REDIS_PORT"] == "6379"


def test_render_does_not_mutate_input():
    config = minimal_config()
    render_config(config, {"REDIS_HOST": "redis"})
    assert config == minimal_config()


def test_validate_lists_every_missing_variable():
    with pytest.raises(ConfigError) as exc:
        validate(minimal_config(), {"LITELLM_MASTER_KEY": "sk-x"})
    assert "AZURE_API_KEY" in str(exc.value)
    assert "LITELLM_SALT_KEY" in str(exc.value)


def test_validate_rejects_master_key_without_sk_prefix():
    env = {**BASE_ENV, "LITELLM_MASTER_KEY": "not-a-key"}
    with pytest.raises(ConfigError, match="sk-"):
        validate(minimal_config(), env)


def test_validate_rejects_empty_model_list():
    with pytest.raises(ConfigError, match="model_list"):
        validate({"model_list": []}, BASE_ENV)


def test_repository_config_is_valid(tmp_path):
    out = prepare(REPO / "config" / "config.yaml", tmp_path / "litellm.yaml", dict(BASE_ENV))
    rendered = yaml.safe_load(out.read_text())
    deployments = rendered["model_list"]
    assert {d["model_name"] for d in deployments} >= {"gpt-5.4", "gpt-5.4-mini"}
    for deployment in deployments:
        params = deployment["litellm_params"]
        assert params["api_key"] == "os.environ/AZURE_API_KEY"
        assert params["api_base"] == "os.environ/AZURE_API_BASE"
        assert params["api_version"] == "os.environ/AZURE_API_VERSION"
        # Sem base_model o custo de deployments Azure não é calculado.
        assert deployment["model_info"]["base_model"].startswith("azure/")
