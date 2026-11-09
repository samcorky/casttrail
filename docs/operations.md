# Running CastTrail

## Run it in Docker (the way it is meant to be run)

```
uv run poe docker-up
```

| Task | Does |
|---|---|
| `poe docker-up` | Builds the image (version taken from git) and starts it, waiting until it is healthy |
| `poe docker-build` | Builds only |
| `poe docker-down` | Stops and removes the container; the data volume and backups stay |
| `poe docker-logs` | Follows the log |
| `poe docker-config` | Prints the compose config as it will run |

- **Settings:** the same `.env` file is passed into the container. The database path and
  backup folder are set for you (`/data` and `/backups` inside the container).
- **Data:** the database lives in the named volume `casttrail-data`. It is not a bind mount
  because SQLite's write-ahead log is unreliable on one from a Windows or macOS host.
- **Backups:** written to `./backups` beside `compose.yaml`, a plain folder you can copy or
  sync elsewhere.
- **Port:** published on `127.0.0.1:8002` only. Put Tailscale in front of it:
  `tailscale serve --bg --https=443 http://127.0.0.1:8002`.
- **Restarts itself:** `restart: unless-stopped` brings it back after a crash or reboot
  (Docker must be set to start with Windows). The container is marked unhealthy, with the
  reason in `docker logs casttrail`, if the app is degraded.
- **Time:** `TZ` (default `Europe/London`) is the clock for the daily backup and refresh.
- **Hardened:** read-only root, non-root user, no capabilities, 512 MB memory limit, logs
  rotated at 10 MB x 3.
- **Update:** `git pull`, then `uv run poe docker-up`. The database is backed up and
  updated on start.
- **Logs:** `docker logs -f casttrail`.

### Moving an existing database into Docker

With the app stopped (so the file is not in use):

```
docker compose up --no-start
docker run --rm -v casttrail_casttrail-data:/data -v "%cd%/data":/from alpine   sh -c "cp /from/casttrail.db /data/ && chown 1000:1000 /data/casttrail.db"
docker compose up -d
```

The volume name is `<folder>_casttrail-data`. Keep the old `data/` folder until you have
checked the new copy.

## Start it without Docker

```
uv run poe serve
```

Settings come from a `.env` file in this folder (copy `.env.example`), or from real
environment variables, which win. Nothing else needs doing: the data folder is created,
the database is brought up to date and a backup is taken, all at startup.

## What happens at startup

1. The data folder is created if it is missing.
2. If the database is behind the code, a copy is taken **first**, then it is updated.
   If the copy cannot be taken, the update is not attempted.
3. If the newest backup is more than a day old (a PC that sleeps at night), one is taken.
4. The first administrator is created if the database has no users and the admin
   settings are set.
5. Timers start: the daily backup, and the Rostercon refresh if switched on.

A problem in step 1 or 2 does not crash the app. It starts, every page shows a plain
"needs attention" message, and `/health` answers `503 {"status": "degraded"}`. The
reason is in the server log. Fix it and restart. Settings that are not valid do the same
and name the setting (never its value). Problems in steps 3 to 5 are warnings, shown on
the admin "Source health" page, and the app works.

## Backups

- **Where:** a `backups` folder beside the database, or `CASTTRAIL_BACKUP_DIR`.
- **When:** before every database update; once a day at `CASTTRAIL_BACKUP_HOUR` (default
  03:30); at startup if the newest is over a day old; and on demand from
  *Admin > Source health > Back up now*.
- **How many:** the newest `CASTTRAIL_BACKUP_KEEP` (default 14) daily copies. Copies
  taken before an update, at startup or by hand are never deleted automatically.
- **Safe while running:** copies use SQLite's own online backup, so the app does not
  need stopping, and a copy only appears under its final name once it is complete.
- **Off:** `CASTTRAIL_BACKUPS_ENABLED=false`. An update is then made without a copy.

Copy the `backups` folder somewhere else now and then. A backup next to the database
does not help if the disk is lost.

## Restore

1. Stop the app.
2. Copy the chosen backup over the database file (`data/casttrail.db` by default), and
   delete `casttrail.db-wal` and `casttrail.db-shm` beside it if they exist.
3. Start the app. If the backup is from an older version it is updated (with a copy
   taken first).

## Settings that matter here

| Setting | Default | Meaning |
|---|---|---|
| `CASTTRAIL_AUTO_MIGRATE` | `true` | Update the database at startup |
| `CASTTRAIL_BACKUPS_ENABLED` | `true` | Take backups |
| `CASTTRAIL_BACKUP_DIR` | beside the database | Where backups go |
| `CASTTRAIL_BACKUP_KEEP` | `14` | Daily copies kept |
| `CASTTRAIL_BACKUP_HOUR` | `3` | Hour of the daily backup (server local time) |
| `CASTTRAIL_REFRESH_ENABLED` | `false` | Contact Rostercon daily |
| `CASTTRAIL_REFRESH_HOUR` | `4` | Hour of the refresh |
