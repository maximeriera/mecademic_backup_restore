# Mecademic Backup/Restore Utility

Command-line tool to back up and restore Mecademic robot variables, files, and writable config sections using a zip archive.

## What it does

- Backs up robot variables to `variables.json`
- Backs up robot files under `files/` inside the archive
- Backs up robot configuration to `robot_config.json`
- Writes metadata and error summary to `manifest.json`
- Restores variables, files, and writable config sections from a previous archive

## Requirements

- Python 3.10+
- Network access to the robot
- Mecademic Python SDK:

```bash
pip install -r requirement.txt
```

## Usage

Run from this repository folder.

### Backup

```bash
python robot_backup_restore.py backup --ip 192.168.0.100 --archive backup_202x-xx-xx.zip
```

Optional flags:

- `--timeout 10.0` operation timeout in seconds
- `--verbose` print per-item actions

### Restore

```bash
python robot_backup_restore.py restore --ip 192.168.0.100 --archive backup_202x-xx-xx.zip
```

Optional flags:

- `--timeout 10.0` operation timeout in seconds
- `--verbose` print per-item actions
- `--dry-run` show restore counts without writing anything to robot
- `--stop-on-error` stop at first restore failure

## Archive contents

A backup zip includes:

- `manifest.json`
- `variables.json`
- `robot_config.json`
- `files/<robot_file_name>`

## Exit codes

- `0`: success
- `2`: completed with one or more item-level errors
- `1`: invalid CLI usage or unknown command

## Notes

- Paths stored inside the archive are normalized and validated to prevent unsafe extraction names.
- Restore defaults to continue on item-level errors unless `--stop-on-error` is used.
