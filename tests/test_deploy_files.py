import re
import tomllib

import pytest
import yaml   # a hard import: a missing pyyaml must fail the run, not skip these tests silently

from app.config import PROJECT_ROOT


def _compose():
    return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_server_mounts_the_key_directory_read_only_and_points_at_the_file():
    server = _compose()["services"]["server"]
    assert "${KEYS_DIR:-./keys}:/keys:ro" in server["volumes"]
    assert "SYNCME_KEYS_FILE=/keys/keys.toml" in server["environment"]


def test_keys_tool_service_is_opt_in_and_can_write_the_key_directory():
    keys = _compose()["services"]["keys"]
    assert keys["profiles"] == ["tools"]
    assert keys["entrypoint"] == ["python", "-m", "app.keys"]
    assert keys["volumes"] == ["${KEYS_DIR:-./keys}:/keys"]
    assert "SYNCME_KEYS_FILE=/keys/keys.toml" in keys["environment"]
    assert keys["user"] == "${KEYS_UID:-1000}:${KEYS_GID:-1000}"
    assert "env_file" not in keys


def test_the_key_directory_is_a_directory_mount_not_a_single_file_mount():
    # a single-file bind mount cannot be replaced atomically by `os.replace`
    for service in _compose()["services"].values():
        for volume in service.get("volumes", []):
            assert ".toml" not in volume


def test_keys_and_env_files_stay_out_of_git_and_the_image():
    gitignore = (PROJECT_ROOT / ".gitignore").read_text().splitlines()
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text().splitlines()
    assert "keys/" in gitignore
    for line in ("keys/", ".env", ".env.*", "!.env.example"):
        assert line in dockerignore
    # the negation must come after the pattern it carves an exception from
    assert dockerignore.index("!.env.example") > dockerignore.index(".env.*")


def test_env_example_no_longer_sets_the_removed_api_key():
    active = [
        line for line in (PROJECT_ROOT / ".env.example").read_text().splitlines() if not line.lstrip().startswith("#")
    ]
    assert not any(line.startswith("SYNCME_API_KEY") for line in active)
    assert "SYNCME_KEYS_FILE" in (PROJECT_ROOT / ".env.example").read_text()


def test_pyyaml_is_declared_for_the_image_and_for_development():
    requirements = [line.strip().lower() for line in (PROJECT_ROOT / "requirements.txt").read_text().splitlines()]
    assert "pyyaml" in requirements
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    assert "pyyaml" in [d.lower() for d in pyproject["project"]["optional-dependencies"]["dev"]]


def _instructions(text):
    """The instruction lines of a Dockerfile: no comments and no blank lines, so a comment cannot satisfy a check."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _assert_locked_down(lines):
    text = "\n".join(lines)
    assert "USER app" in lines
    assert "uv sync --frozen --no-dev --no-install-project" in text
    assert re.search(r"pip install --no-cache-dir uv==\d+\.\d+\.\d+", text)       # pinned, not "latest"
    assert "COPY . ." not in text
    assert "PYTHONDONTWRITEBYTECODE=1" in text
    assert "pytest" not in text
    assert "requirements.txt" not in text                                           # the lock file is the source of truth


def test_dockerfile_is_locked_down():
    _assert_locked_down(_instructions((PROJECT_ROOT / "Dockerfile").read_text()))


def test_a_commented_out_instruction_does_not_count():
    sample = "FROM python:3.12\n# USER app\n    #USER app\n\nRUN true\n"
    assert "USER app" not in _instructions(sample)
    assert "USER app" not in "\n".join(_instructions(sample))         # not even as a substring
    assert "USER app" in _instructions(sample + "USER app\n")


@pytest.mark.parametrize(
    "needle", ["USER app", "uv sync --frozen --no-dev --no-install-project", "PYTHONDONTWRITEBYTECODE=1"]
)
def test_commenting_out_a_required_instruction_fails_the_locked_down_check(needle):
    real = (PROJECT_ROOT / "Dockerfile").read_text().splitlines()
    commented = ["# " + line if needle in line else line for line in real]
    assert commented != real
    with pytest.raises(AssertionError):
        _assert_locked_down(_instructions("\n".join(commented)))


def test_dockerfile_copies_only_what_runs():
    copies = [line for line in _instructions((PROJECT_ROOT / "Dockerfile").read_text()) if line.startswith("COPY")]
    assert "COPY app ./app" in copies and "COPY log_config.json ./" in copies
    assert "COPY pyproject.toml uv.lock ./" in copies
    assert "COPY --from=build /app/.venv /app/.venv" in copies


@pytest.mark.parametrize("service", ["server", "keys", "backup"])
def test_compose_service_is_hardened(service):
    svc = _compose()["services"][service]
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["read_only"] is True
    assert svc["tmpfs"] == ["/tmp"]


def test_server_runs_as_a_configurable_non_root_user_by_default():
    svc = _compose()["services"]["server"]
    assert svc["user"] == "${SERVER_UID:-1000}:${SERVER_GID:-1000}"


def test_compose_has_no_obsolete_version_key():
    assert "version" not in _compose()


def test_dockerignore_keeps_secrets_and_state_out_of_the_image():
    lines = set((PROJECT_ROOT / ".dockerignore").read_text().split())
    assert {
        "keys/", ".env", ".env.*", "!.env.example", ".venv/", "data/", "storage/", ".git/", "tests/", "*.egg-info/",
    } <= lines


def test_readme_documents_the_rollback_and_the_limits():
    readme = (PROJECT_ROOT / "README.md").read_text()
    assert "SERVER_UID=0" in readme and "SERVER_GID=0" in readme
    assert "MAX_UPLOAD_BYTES" in readme
    assert "install -d -o 1000 -g 1000 -m 700 keys" in readme and "mkdir -p keys" not in readme


def test_readme_rollback_is_not_just_the_uid():
    # cap_drop ALL stays: root has no CAP_DAC_OVERRIDE and cannot touch files owned by uid 1000
    readme = (PROJECT_ROOT / "README.md").read_text()
    assert "restore the old behaviour" not in readme
    assert "chown -R 0:0" in readme and "cap_add: [DAC_OVERRIDE]" in readme
    assert "`KEYS_UID`/`KEYS_GID` must match the owner of `keys/keys.toml`" in readme


def test_readme_states_the_known_limits():
    readme = (PROJECT_ROOT / "README.md").read_text()
    assert "### Known limits" in readme
    section = readme.split("### Known limits")[1].split("\n### ")[0]
    assert "os.replace" in section and "re-index" in section and "rolled back" in section
    assert "requirements.txt" in section and "pyproject.toml" in section and "uv.lock" in section


def test_readme_path_limits_match_the_code():
    from app.validation import MAX_COMPONENT_BYTES, MAX_PATH_BYTES

    readme = (PROJECT_ROOT / "README.md").read_text()
    assert f"each component at most {MAX_COMPONENT_BYTES} UTF-8 bytes" in readme
    assert f"the whole path at most {MAX_PATH_BYTES} bytes" in readme


def test_backup_tool_service_is_opt_in_and_mounts_what_backup_and_restore_need():
    svc = _compose()["services"]["backup"]
    assert svc["profiles"] == ["tools"]
    assert svc["entrypoint"] == ["python", "-m", "app.cli"]
    # restore writes data, storage and (with --include-keys) the key file; restored files must belong to the server
    assert svc["user"] == "${SERVER_UID:-1000}:${SERVER_GID:-1000}"
    assert set(svc["volumes"]) == {
        "${DATA_DIR:-./data}:/data",
        "${STORAGE_DIR:-./storage}:/storage",
        "${KEYS_DIR:-./keys}:/keys",
        "${BACKUP_DIR:-./backups}:/backups",
    }
    env = svc["environment"]
    for line in ("DATA_DIR=/data", "STORAGE_DIR=/storage", "SYNCME_KEYS_FILE=/keys/keys.toml", "BACKUP_DIR=/backups",
                 "BACKUP_KEEP=${BACKUP_KEEP:-0}"):
        assert line in env
    assert svc["env_file"] == [".env"]     # the same path overrides (ARCHIVE_DIR, DB_PATH) as the server


def test_backups_stay_out_of_git_and_the_image():
    assert "backups/" in (PROJECT_ROOT / ".gitignore").read_text().splitlines()
    assert "backups/" in (PROJECT_ROOT / ".dockerignore").read_text().splitlines()


def test_env_example_documents_the_backup_settings():
    text = (PROJECT_ROOT / ".env.example").read_text()
    assert "BACKUP_DIR" in text and "BACKUP_KEEP=7" in text


def test_systemd_units_run_the_backup_tool_service():
    service = (PROJECT_ROOT / "systemd" / "syncme-backup.service").read_text()
    timer = (PROJECT_ROOT / "systemd" / "syncme-backup.timer").read_text()
    assert "Type=oneshot" in service
    assert "compose run --build --rm backup backup" in service
    assert "OnCalendar=" in timer and "Persistent=true" in timer
    assert "Unit=syncme-backup.service" in timer


def test_readme_documents_backup_and_restore():
    readme = (PROJECT_ROOT / "README.md").read_text()
    for needle in ("docker compose run --rm backup backup", "docker compose run --rm backup restore",
                   "--include-keys", "--force", "syncme-client init", "BACKUP_KEEP", ".pre-restore-",
                   "verify-backup"):
        assert needle in readme


def test_docs_say_how_to_rebuild_the_tool_images_after_an_update():
    # `docker compose build` and `up --build` skip services in an inactive profile, and `run` reuses an
    # existing image: without this step the keys/cli/backup tools keep running the old code.
    for doc in ("README.md", "TLDR.md"):
        assert "docker compose --profile tools build" in (PROJECT_ROOT / doc).read_text()
    readme = (PROJECT_ROOT / "README.md").read_text()
    assert "run --build --rm backup prune --keep-days 14 --archive-days 14" in readme      # the cron line
