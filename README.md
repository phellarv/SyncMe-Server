# SyncMe-Server

## TL;DR

Central server for SyncMe. Stores every file revision per workspace, rejects stale writes with `409`, and pushes changes to clients over SSE. Each client has its own API key.

```bash
cp .env.example .env
install -d -o 1000 -g 1000 -m 700 keys    # key directory, owned by the container user
docker compose run --rm keys add laptop    # prints the client key once; copy it
docker compose up -d --build               # listens on :8000; put HTTPS in front of it
curl -s localhost:8000/health              # {"status":"ok"}
```

Without Docker: `pip install -e ".[dev]"`, `python -m app.keys add <name>`, `uvicorn app.main:app`.
More: [cheat sheet](./TLDR.md) · [SyncMe user guide](https://github.com/phellarv/SyncMe/blob/main/docs/README.md).

---

## Documentation

The full guide lives in the [SyncMe repository](https://github.com/phellarv/SyncMe), which ties server and client together:

- [Setup](https://github.com/phellarv/SyncMe/blob/main/docs/setup.md): server and client from scratch
- [Configuration](https://github.com/phellarv/SyncMe/blob/main/docs/configuration.md): `config.toml`, environment variables, `.smignore`
- [Usage](https://github.com/phellarv/SyncMe/blob/main/docs/usage.md): daily CLI use, conflicts, history, Waybar, exit codes
- [Operations](https://github.com/phellarv/SyncMe/blob/main/docs/operations.md): keys, status, backup and restore, pruning, upgrades
- [Reference](https://github.com/phellarv/SyncMe/blob/main/docs/reference.md): architecture, storage, database schema, REST API

## Operator essentials

The full operator guide is [Operations](https://github.com/phellarv/SyncMe/blob/main/docs/operations.md). These are the points not to miss.

### Upgrading

```bash
git pull
docker compose --profile tools build   # also the keys, cli and backup tool images
docker compose up -d server
```

`docker compose build` and `up --build` skip the tool services (they are in the `tools` profile), and `docker compose run` reuses an existing image. Without the first build line the keys, cli and backup tools keep running old code.

### Backup & Restore

```bash
install -d -o 1000 -g 1000 -m 700 backups                                # once; owner = SERVER_UID/SERVER_GID
docker compose run --rm backup prune --keep-days 14 --archive-days 14    # before the backup, never at the same time
docker compose run --rm backup backup                                    # --include-keys adds keys.toml (plain-text tokens)
docker compose run --rm backup verify-backup /backups/<archive>.tar.gz
```

- Retention: `BACKUP_KEEP` in `.env` (or `--keep N`); `0` keeps all archives. Archives are not encrypted; keep them off the data disk.
- `systemd/syncme-backup.timer` runs prune and backup every night. With cron instead:
  ```
  15 3 * * * cd /opt/SyncMe-Server && (docker compose run --build --rm backup prune --keep-days 14 --archive-days 14; docker compose run --build --rm backup backup) >> /var/log/syncme-backup.log 2>&1
  ```

Restore, with the server stopped:

```bash
docker compose stop server
docker compose run --rm backup restore /backups/<archive>.tar.gz --force
docker compose start server
```

- `--force` is needed over existing data. The old data is moved aside to `.pre-restore-<UTC time>/`; delete it once the restore is confirmed.
- Changes made after the backup exist only on the clients. Run `syncme-client init` on each client to reconcile them.

### Running as root again

`SERVER_UID=0` and `SERVER_GID=0` in `.env` are not enough on their own: all capabilities stay dropped, so root cannot read or write files owned by uid 1000. Also run `chown -R 0:0 <data dir> <storage dir>` (and the key directory), or temporarily add `cap_add: [DAC_OVERRIDE]` to the `server` service. `KEYS_UID`/`KEYS_GID` must match the owner of `keys/keys.toml`, otherwise the `keys` service cannot update the file.

### Limits

- `MAX_UPLOAD_BYTES` (default 256 MiB): larger uploads get `413`.
- Paths: each component at most 230 UTF-8 bytes, the whole path at most 3500 bytes.
- At most 10 concurrent event streams per client (`429` beyond that).

### Known limits

- A failure of the SQLite commit after a successful `os.replace` leaves the new file in place while the state row is rolled back. The next restart re-indexes the file as a new revision.
- `requirements.txt` is no longer used by the Docker image: `pyproject.toml` and `uv.lock` are the source of the dependencies.

## Development & Testing

SyncMe-Server uses `pytest` and `pytest-asyncio` for test execution.

Run the test suite:
```bash
uv run pytest
# or with active virtual environment:
pytest tests/ -v
```
