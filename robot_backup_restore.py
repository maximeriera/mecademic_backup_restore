"""Backup and restore Mecademic robot variables and files into a zip archive.

This tool uses mecademicpy to:
- backup: export all robot variables + all robot files into one zip
- restore: re-upload variables + files from a previous zip backup
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import PurePosixPath
import sys
import time
import zipfile
from typing import Any, Dict, Iterable, List, Tuple

import mecademicpy.robot as mdr

SCHEMA_VERSION = 2
MANIFEST_NAME = "manifest.json"
VARIABLES_NAME = "variables.json"
ROBOT_CONFIG_NAME = "robot_config.json"
FILES_PREFIX = "files/"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_json_serializable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


@contextmanager
def connected_robot(ip: str, timeout: float):
    robot = mdr.Robot()
    robot.Connect(
        address=ip,
        enable_synchronous_mode=True,
        disconnect_on_exception=False,
        timeout=timeout,
    )
    try:
        yield robot
    finally:
        if robot.IsConnected():
            robot.Disconnect()


def ensure_variable_sync(robot: mdr.Robot, timeout: float, verbose: bool) -> None:
    """Best-effort synchronization of robot variables into the local cache.

    `ListVariables` reads from the local synchronized map. On some firmware/connection
    combinations, variable monitoring may not be enabled by default for user apps.
    """
    try:
        # Enable dictionary updates too so variable metadata remains populated.
        robot.SendCustomCommand("-SetDictMonitoring(1,0)", expected_responses=None, timeout=None)

        # Enable variable monitoring updates when supported.
        robot.SendCustomCommand("-SetVariablesMonitoring(1)", expected_responses=None, timeout=None)
    except Exception as exc:  # pylint: disable=broad-except
        if verbose:
            print(f"[backup:warn] Could not enable variable monitoring: {exc}")

    try:
        # Trigger a sync point so any pending monitoring messages are processed.
        robot.SyncCmdQueue()
    except Exception as exc:  # pylint: disable=broad-except
        if verbose:
            print(f"[backup:warn] SyncCmdQueue failed while syncing variables: {exc}")

    # In practice, variables usually appear immediately after monitoring is enabled.
    # Retry a few short times to cover delayed first updates.
    attempts = 5
    for _ in range(attempts):
        if robot.ListVariables():
            break
        try:
            robot.GetStatusRobot(timeout=timeout)
        except Exception:
            pass
        time.sleep(0.2)


def normalize_archive_file_name(robot_file_name: str) -> str:
    # Store robot files under a stable POSIX-like path in the zip archive.
    normalized = str(PurePosixPath(robot_file_name.replace("\\", "/")))
    return f"{FILES_PREFIX}{normalized}"


def denormalize_archive_file_name(archive_name: str) -> str:
    relative = archive_name[len(FILES_PREFIX):]
    normalized = str(PurePosixPath(relative))

    # Block unsafe names (zip-slip style paths).
    if not normalized or normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
        raise ValueError(f"Unsafe archive path: {archive_name}")

    return normalized


def serialize_variable(var_obj: Any) -> Dict[str, Any]:
    value = var_obj.get_value()

    if is_json_serializable(value):
        encoded_value = value
        value_encoding = "json"
    else:
        encoded_value = repr(value)
        value_encoding = "repr"

    cyclic_id = var_obj.cyclic_id
    if cyclic_id == 0:
        cyclic_id = None

    return {
        "cyclic_id": cyclic_id,
        "volatile": bool(var_obj.volatile),
        "description": var_obj.description,
        "value": encoded_value,
        "value_encoding": value_encoding,
    }


def decode_variable_value(var_name: str, var_payload: Dict[str, Any]) -> Any:
    if "value_encoding" in var_payload:
        encoding = var_payload.get("value_encoding")
        if encoding == "json":
            return var_payload.get("value")
        if encoding == "repr":
            # repr fallback cannot be safely deserialized, keep textual value.
            return var_payload.get("value")
        raise ValueError(f"Unsupported value_encoding '{encoding}' for variable '{var_name}'")

    # Legacy compatibility (existing variable_list.json style):
    # value may be a JSON string payload or already decoded value.
    raw_value = var_payload.get("value")
    if isinstance(raw_value, str):
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            return raw_value
    return raw_value


def to_jsonable(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return int(value)
    if dataclasses.is_dataclass(value):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def collect_robot_config(robot: mdr.Robot, timeout: float, errors: List[Dict[str, str]]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
    }

    robot_info = None
    try:
        robot_info = robot.GetRobotInfo()
        payload["robot_info"] = to_jsonable(robot_info)
    except Exception as exc:  # pylint: disable=broad-except
        errors.append({"kind": "config_export", "name": "robot_info", "error": str(exc)})

    try:
        payload["network_config"] = to_jsonable(robot.GetNetworkCfg(timeout=timeout))
    except Exception as exc:  # pylint: disable=broad-except
        errors.append({"kind": "config_export", "name": "network_config", "error": str(exc)})

    try:
        payload["network_options"] = to_jsonable(robot.GetNetworkOptions(timeout=timeout))
    except Exception as exc:  # pylint: disable=broad-except
        errors.append({"kind": "config_export", "name": "network_options", "error": str(exc)})

    try:
        payload["protocol_modes"] = {
            "ethernet_ip_enabled": bool(robot.GetEtherNetIpEnabled(timeout=timeout)),
            "profinet_enabled": bool(robot.GetProfinetEnabled(timeout=timeout)),
        }
    except Exception as exc:  # pylint: disable=broad-except
        errors.append({"kind": "config_export", "name": "protocol_modes", "error": str(exc)})

    try:
        num_joints = int(getattr(robot_info, "num_joints", 6))
        joint_cfg = robot.GetJointLimitsCfg(timeout=timeout)
        joint_limits: Dict[str, Dict[str, float]] = {}
        for joint in range(1, num_joints + 1):
            lower, upper = robot.GetJointLimits(joint, effective=False, timeout=timeout)
            joint_limits[str(joint)] = {"lower": float(lower), "upper": float(upper)}

        payload["joint_limits"] = {
            "cfg": to_jsonable(joint_cfg),
            "limits": joint_limits,
        }
    except Exception as exc:  # pylint: disable=broad-except
        errors.append({"kind": "config_export", "name": "joint_limits", "error": str(exc)})

    try:
        payload["work_zone"] = {
            "cfg": to_jsonable(robot.GetWorkZoneCfg(timeout=timeout)),
            "limits": to_jsonable(robot.GetWorkZoneLimits(timeout=timeout)),
            "status": to_jsonable(robot.GetWorkZoneStatus(synchronous_update=True, timeout=timeout)),
        }
    except Exception as exc:  # pylint: disable=broad-except
        errors.append({"kind": "config_export", "name": "work_zone", "error": str(exc)})

    try:
        payload["collision"] = {
            "cfg": to_jsonable(robot.GetCollisionCfg(timeout=timeout)),
            "status": to_jsonable(robot.GetCollisionStatus(synchronous_update=True, timeout=timeout)),
        }
    except Exception as exc:  # pylint: disable=broad-except
        errors.append({"kind": "config_export", "name": "collision", "error": str(exc)})

    payload["restore_writable"] = {
        "network_options": True,
        "protocol_modes": True,
        "joint_limits": True,
        "work_zone": True,
        "collision": True,
        "robot_info": False,
        "network_config": False,
    }
    return payload


def load_robot_config_payload(archive: zipfile.ZipFile) -> Dict[str, Any]:
    if ROBOT_CONFIG_NAME not in archive.namelist():
        return {}
    raw = archive.read(ROBOT_CONFIG_NAME).decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Invalid robot config payload: expected dictionary")
    return payload


def restore_robot_config(
    robot: mdr.Robot,
    config_payload: Dict[str, Any],
    timeout: float,
    verbose: bool,
    continue_on_error: bool,
    errors: List[Dict[str, str]],
) -> int:
    restored = 0

    def record_restore_error(section: str, exc: Exception):
        errors.append({
            "kind": "config_restore",
            "name": section,
            "error": str(exc),
        })

    network_options = config_payload.get("network_options")
    if isinstance(network_options, dict) and "keep_alive_timeout" in network_options:
        try:
            robot.SetNetworkOptions(int(network_options["keep_alive_timeout"]))
            restored += 1
            if verbose:
                print("[restore:config] network_options")
        except Exception as exc:  # pylint: disable=broad-except
            record_restore_error("network_options", exc)
            if not continue_on_error:
                return restored

    protocol_modes = config_payload.get("protocol_modes")
    if isinstance(protocol_modes, dict):
        try:
            if "ethernet_ip_enabled" in protocol_modes:
                robot.EnableEtherNetIp(bool(protocol_modes["ethernet_ip_enabled"]))
            if "profinet_enabled" in protocol_modes:
                robot.EnableProfinet(bool(protocol_modes["profinet_enabled"]))
            restored += 1
            if verbose:
                print("[restore:config] protocol_modes")
        except Exception as exc:  # pylint: disable=broad-except
            record_restore_error("protocol_modes", exc)
            if not continue_on_error:
                return restored

    joint_limits = config_payload.get("joint_limits")
    if isinstance(joint_limits, dict):
        limits = joint_limits.get("limits", {})
        try:
            for joint_str in sorted(limits.keys(), key=lambda x: int(x)):
                limit = limits[joint_str]
                robot.SetJointLimits(int(joint_str), float(limit["lower"]), float(limit["upper"]))

            cfg = joint_limits.get("cfg", {})
            if isinstance(cfg, dict) and "enabled" in cfg:
                robot.SetJointLimitsCfg(bool(cfg["enabled"]))

            restored += 1
            if verbose:
                print("[restore:config] joint_limits")
        except Exception as exc:  # pylint: disable=broad-except
            record_restore_error("joint_limits", exc)
            if not continue_on_error:
                return restored

    work_zone = config_payload.get("work_zone")
    if isinstance(work_zone, dict):
        try:
            limits = work_zone.get("limits", {})
            if isinstance(limits, dict):
                robot.SetWorkZoneLimits(
                    float(limits["x_min"]),
                    float(limits["y_min"]),
                    float(limits["z_min"]),
                    float(limits["x_max"]),
                    float(limits["y_max"]),
                    float(limits["z_max"]),
                )

            cfg = work_zone.get("cfg", {})
            if isinstance(cfg, dict) and "severity" in cfg and "mode" in cfg:
                robot.SetWorkZoneCfg(mdr.MxEventSeverity(int(cfg["severity"])), mdr.MxWorkZoneMode(int(cfg["mode"])))

            restored += 1
            if verbose:
                print("[restore:config] work_zone")
        except Exception as exc:  # pylint: disable=broad-except
            record_restore_error("work_zone", exc)
            if not continue_on_error:
                return restored

    collision = config_payload.get("collision")
    if isinstance(collision, dict):
        try:
            cfg = collision.get("cfg", {})
            if isinstance(cfg, dict) and "severity" in cfg:
                robot.SetCollisionCfg(mdr.MxEventSeverity(int(cfg["severity"])))
                restored += 1
                if verbose:
                    print("[restore:config] collision")
        except Exception as exc:  # pylint: disable=broad-except
            record_restore_error("collision", exc)
            if not continue_on_error:
                return restored

    return restored


def backup_robot(ip: str, archive_path: str, timeout: float, verbose: bool) -> int:
    errors: List[Dict[str, str]] = []
    variables: Dict[str, Dict[str, Any]] = {}
    file_records: List[Dict[str, Any]] = []
    config_payload: Dict[str, Any] = {}

    with connected_robot(ip, timeout) as robot:
        ensure_variable_sync(robot, timeout=timeout, verbose=verbose)

        # Variables backup
        variable_names = sorted(robot.ListVariables())
        for var_name in variable_names:
            try:
                var_obj = robot.GetVariable(var_name)
                if var_obj is None:
                    raise RuntimeError("GetVariable returned None")
                variables[var_name] = serialize_variable(var_obj)
            except Exception as exc:  # pylint: disable=broad-except
                errors.append({
                    "kind": "variable_export",
                    "name": var_name,
                    "error": str(exc),
                })

        # Files backup
        robot_files = robot.ListFiles(timeout=timeout)
        file_names = sorted(robot_files.files.keys())

        # Robot config backup (includes writable + read-only sections)
        config_payload = collect_robot_config(robot, timeout=timeout, errors=errors)

        with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_name in file_names:
                try:
                    loaded_file = robot.LoadFile(file_name, timeout=timeout)
                    content = loaded_file.content

                    if isinstance(content, str):
                        raw_bytes = content.encode("utf-8")
                        content_encoding = "utf-8"
                    elif isinstance(content, (bytes, bytearray)):
                        raw_bytes = bytes(content)
                        content_encoding = "bytes"
                    else:
                        raise TypeError(f"Unsupported file content type: {type(content).__name__}")

                    archive_name = normalize_archive_file_name(file_name)
                    archive.writestr(archive_name, raw_bytes)

                    file_records.append({
                        "name": file_name,
                        "archive_name": archive_name,
                        "content_encoding": content_encoding,
                        "size": len(raw_bytes),
                    })

                    if verbose:
                        print(f"[backup:file] {file_name} -> {archive_name} ({len(raw_bytes)} bytes)")
                except Exception as exc:  # pylint: disable=broad-except
                    errors.append({
                        "kind": "file_export",
                        "name": file_name,
                        "error": str(exc),
                    })

            variables_payload = {
                "schema_version": SCHEMA_VERSION,
                "variables": variables,
            }

            manifest = {
                "schema_version": SCHEMA_VERSION,
                "created_at_utc": utc_now_iso(),
                "source": {
                    "ip": ip,
                },
                "counts": {
                    "variables_total": len(variable_names),
                    "variables_exported": len(variables),
                    "files_total": len(file_names),
                    "files_exported": len(file_records),
                    "config_sections_exported": len([k for k in config_payload.keys() if k not in ("schema_version", "restore_writable")]),
                    "errors": len(errors),
                },
                "files": file_records,
                "errors": errors,
            }

            robot_info = config_payload.get("robot_info", {})
            if isinstance(robot_info, dict):
                manifest["robot_info"] = {
                    "model": robot_info.get("model"),
                    "revision": robot_info.get("revision"),
                    "serial": robot_info.get("serial"),
                    "version": robot_info.get("version", {}).get("full_version") if isinstance(robot_info.get("version"), dict) else robot_info.get("version"),
                }

            archive.writestr(VARIABLES_NAME, json.dumps(variables_payload, indent=2))
            archive.writestr(ROBOT_CONFIG_NAME, json.dumps(config_payload, indent=2))
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))

    print(f"Backup complete: {archive_path}")
    print(f"Variables exported: {len(variables)}")
    print(f"Files exported: {len(file_records)}")
    print(f"Config exported: {ROBOT_CONFIG_NAME}")
    print(f"Errors: {len(errors)}")

    return 0 if not errors else 2


def load_variables_payload(archive: zipfile.ZipFile) -> Dict[str, Dict[str, Any]]:
    raw = archive.read(VARIABLES_NAME).decode("utf-8")
    payload = json.loads(raw)

    if isinstance(payload, dict) and "variables" in payload:
        variables = payload["variables"]
    else:
        # Legacy compatibility: file content is directly the variables dictionary.
        variables = payload

    if not isinstance(variables, dict):
        raise ValueError("Invalid variables payload: expected dictionary")

    return variables


def iter_archived_files(archive: zipfile.ZipFile) -> Iterable[Tuple[str, bytes]]:
    for info in archive.infolist():
        name = info.filename
        if info.is_dir():
            continue
        if not name.startswith(FILES_PREFIX):
            continue

        robot_name = denormalize_archive_file_name(name)
        yield robot_name, archive.read(name)


def restore_robot(
    ip: str,
    archive_path: str,
    timeout: float,
    verbose: bool,
    dry_run: bool,
    continue_on_error: bool,
) -> int:
    errors: List[Dict[str, str]] = []
    restored_vars = 0
    restored_files = 0
    restored_config = 0

    with zipfile.ZipFile(archive_path, mode="r") as archive:
        if VARIABLES_NAME not in archive.namelist():
            raise FileNotFoundError(f"Missing '{VARIABLES_NAME}' in archive")

        variables = load_variables_payload(archive)
        files_from_archive = list(iter_archived_files(archive))
        config_payload = load_robot_config_payload(archive)

    if dry_run:
        print("[dry-run] Restore preview")
        print(f"Variables to restore: {len(variables)}")
        print(f"Files to restore: {len(files_from_archive)}")
        writable_config_items = 0
        if config_payload.get("network_options") is not None:
            writable_config_items += 1
        if config_payload.get("protocol_modes") is not None:
            writable_config_items += 1
        if config_payload.get("joint_limits") is not None:
            writable_config_items += 1
        if config_payload.get("work_zone") is not None:
            writable_config_items += 1
        if config_payload.get("collision") is not None:
            writable_config_items += 1
        print(f"Writable config sections to restore: {writable_config_items}")
        return 0

    with connected_robot(ip, timeout) as robot:
        for var_name, var_payload in variables.items():
            try:
                value = decode_variable_value(var_name, var_payload)
                cyclic_id = var_payload.get("cyclic_id")
                if cyclic_id in (0, "0"):
                    cyclic_id = None

                robot.CreateVariable(
                    name=var_name,
                    value=value,
                    cyclic_id=cyclic_id,
                    volatile=bool(var_payload.get("volatile", False)),
                    override=True,
                    timeout=timeout,
                )
                restored_vars += 1
                if verbose:
                    print(f"[restore:variable] {var_name}")
            except Exception as exc:  # pylint: disable=broad-except
                errors.append({
                    "kind": "variable_restore",
                    "name": var_name,
                    "error": str(exc),
                })
                if not continue_on_error:
                    break

        if not errors or continue_on_error:
            for file_name, raw_bytes in files_from_archive:
                try:
                    content = raw_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    content = raw_bytes.decode("latin-1")

                try:
                    robot.SaveFile(
                        name=file_name,
                        content=content,
                        timeout=timeout,
                        allow_invalid=True,
                        overwrite=True,
                    )
                    restored_files += 1
                    if verbose:
                        print(f"[restore:file] {file_name}")
                except Exception as exc:  # pylint: disable=broad-except
                    errors.append({
                        "kind": "file_restore",
                        "name": file_name,
                        "error": str(exc),
                    })
                    if not continue_on_error:
                        break

        if not errors or continue_on_error:
            restored_config = restore_robot_config(
                robot=robot,
                config_payload=config_payload,
                timeout=timeout,
                verbose=verbose,
                continue_on_error=continue_on_error,
                errors=errors,
            )

    print(f"Restore complete from: {archive_path}")
    print(f"Variables restored: {restored_vars}")
    print(f"Files restored: {restored_files}")
    print(f"Writable config sections restored: {restored_config}")
    print(f"Errors: {len(errors)}")

    if errors:
        print("Failure details:")
        for item in errors:
            print(f"- [{item['kind']}] {item['name']}: {item['error']}")

    return 0 if not errors else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backup and restore Mecademic robot variables/files using zip archives.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="Create a robot backup archive")
    backup_parser.add_argument("--ip", required=True, help="Robot IP address")
    backup_parser.add_argument("--archive", required=True, help="Output zip archive path")
    backup_parser.add_argument("--timeout", type=float, default=10.0, help="Operation timeout in seconds")
    backup_parser.add_argument("--verbose", action="store_true", help="Print per-item operations")

    restore_parser = subparsers.add_parser("restore", help="Restore robot data from archive")
    restore_parser.add_argument("--ip", required=True, help="Robot IP address")
    restore_parser.add_argument("--archive", required=True, help="Input zip archive path")
    restore_parser.add_argument("--timeout", type=float, default=10.0, help="Operation timeout in seconds")
    restore_parser.add_argument("--verbose", action="store_true", help="Print per-item operations")
    restore_parser.add_argument("--dry-run", action="store_true", help="Only print what would be restored")
    restore_parser.add_argument(
        "--continue-on-error",
        action="store_true",
        default=True,
        help="Keep restoring remaining items even after an error",
    )
    restore_parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop restore on first error (overrides default continue behavior)",
    )

    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "backup":
        return backup_robot(
            ip=args.ip,
            archive_path=args.archive,
            timeout=args.timeout,
            verbose=args.verbose,
        )

    if args.command == "restore":
        return restore_robot(
            ip=args.ip,
            archive_path=args.archive,
            timeout=args.timeout,
            verbose=args.verbose,
            dry_run=args.dry_run,
            continue_on_error=(args.continue_on_error and not args.stop_on_error),
        )

    parser.error(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
