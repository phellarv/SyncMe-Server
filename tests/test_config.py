import pytest
import os
from pathlib import Path
from pydantic import ValidationError
from app.config import Settings, PROJECT_ROOT


ENV_VARS_TO_CLEAR = [
    "SYNCME_API_KEY",
    "DATA_DIR",
    "STORAGE_DIR",
    "FILES_DIR",
    "ARCHIVE_DIR",
    "DB_PATH",
    "HOST",
    "PORT",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure environment variables do not bleed into Settings tests."""
    for var in ENV_VARS_TO_CLEAR:
        monkeypatch.delenv(var, raising=False)


def test_settings_loads_values_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SYNCME_API_KEY=test-secret-key-456\n"
        "PORT=9090\n"
        "DATA_DIR=/tmp/custom_data\n"
        "HOST=127.0.0.1\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)
    assert settings.SYNCME_API_KEY == "test-secret-key-456"
    assert settings.PORT == 9090
    assert settings.DATA_DIR == Path("/tmp/custom_data")
    assert settings.HOST == "127.0.0.1"


def test_settings_ignores_extra_unknown_keys(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SYNCME_API_KEY=my-valid-key\n"
        "PORT=8888\n"
        "EXTRA_UNKNOWN_OPTION=unexpected_value\n"
        "ANOTHER_RANDOM_SETTING=12345\n",
        encoding="utf-8",
    )

    # Should not raise pydantic.ValidationError despite unknown keys
    settings = Settings(_env_file=env_file)
    assert settings.SYNCME_API_KEY == "my-valid-key"
    assert settings.PORT == 8888
    assert not hasattr(settings, "EXTRA_UNKNOWN_OPTION")


def test_settings_loads_project_root_env_from_different_cwd(tmp_path, monkeypatch):
    # Never touch PROJECT_ROOT/.env: that is a real, user-owned file. Assert on the configuration
    # (the project-root path is searched regardless of the cwd) and load values from a file in tmp_path.
    env_files = Settings.model_config["env_file"]
    assert PROJECT_ROOT / ".env" in env_files
    assert PROJECT_ROOT.is_absolute()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert Path.cwd() != PROJECT_ROOT
    assert not (Path.cwd() / ".env").exists()

    settings_file = tmp_path / "root-level-settings.cfg"
    settings_file.write_text(
        "SYNCME_API_KEY=root-level-env-key\n"
        "PORT=7654\n"
        "DATA_DIR=/tmp/root_data\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=settings_file)
    assert settings.SYNCME_API_KEY == "root-level-env-key"
    assert settings.PORT == 7654
    assert settings.DATA_DIR == Path("/tmp/root_data")


def test_settings_default_values():
    settings = Settings(_env_file=None)
    assert settings.SYNCME_API_KEY is None
    assert settings.HOST == "0.0.0.0"
    assert settings.PORT == 8000
    assert settings.DATA_DIR == Path("./data")


def test_settings_and_storage_dir_from_env(tmp_path):
    storage_dir = tmp_path / "custom_storage"
    env_file = tmp_path / ".env"
    env_file.write_text(f"STORAGE_DIR={storage_dir}\n", encoding="utf-8")

    settings = Settings(_env_file=env_file)
    assert settings.STORAGE_DIR == storage_dir

    with pytest.MonkeyPatch.context() as mp:
        import app.storage as storage
        import app.config as config
        mp.setattr(storage, "settings", settings)
        mp.setattr(config, "settings", settings)
        assert storage.get_files_dir() == storage_dir.resolve()
        assert storage_dir.is_dir()


def test_docker_compose_and_dockerfile_port_configuration():
    docker_compose_path = PROJECT_ROOT / "docker-compose.yml"
    assert docker_compose_path.exists()
    dc_content = docker_compose_path.read_text(encoding="utf-8")
    assert "${HOST:-0.0.0.0}:${PORT:-8000}:${PORT:-8000}" in dc_content
    assert "PORT=${PORT:-8000}" in dc_content

    dockerfile_path = PROJECT_ROOT / "Dockerfile"
    assert dockerfile_path.exists()
    dockerfile_content = dockerfile_path.read_text(encoding="utf-8")
    assert "ENV PORT=8000" in dockerfile_content
    assert 'CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --log-config log_config.json"]' in dockerfile_content
