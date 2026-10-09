import argparse
import datetime
import sys
import time
from pathlib import Path

from . import db


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def format_time(ts: float | None) -> str:
    if not ts or ts <= 0:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_status(last_seen: float | None, is_active: bool = False, now: float | None = None) -> str:
    if not last_seen or last_seen <= 0:
        return "Never connected"
    if now is None:
        now = time.time()
    diff = max(0.0, now - last_seen)
    if is_active or diff < 120:
        return "Online (SSE)"
    if diff < 3600:
        return f"Idle ({int(diff // 60)}m ago)"
    if diff < 86400:
        return f"Idle ({int(diff // 3600)}h ago)"
    return f"Offline ({int(diff // 86400)}d ago)"


def _clean_cell(val: object) -> str:
    s = str(val if val is not None else "")
    return "".join(c if c.isprintable() else "?" for c in s)


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    cleaned_rows = [[_clean_cell(cell) for cell in row] for row in rows]
    col_widths = [len(h) for h in headers]
    for row in cleaned_rows:
        for i, val in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(val))

    header_line = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    sep_line = "-+-".join("-" * col_widths[i] for i in range(len(headers)))

    lines = [header_line, sep_line]
    for row in cleaned_rows:
        row_line = " | ".join(str(row[i] if i < len(row) else "").ljust(col_widths[i]) for i in range(len(headers)))
        lines.append(row_line)
    return "\n".join(lines)


def load_all_clients() -> tuple[dict[str, dict], list[dict]]:
    clients_from_keys = {}
    try:
        from .keys import keys_path, read_clients
        p = keys_path()
        if p.exists():
            clients_from_keys = read_clients(p)
    except Exception:
        pass

    db_state = db.get_clients_state()
    return clients_from_keys, db_state


def is_client_active(name: str) -> bool:
    try:
        from . import events
        return events.broadcaster.count_for(name) > 0
    except Exception:
        return False


def cmd_clients() -> int:
    keys_clients, db_state = load_all_clients()

    client_workspaces: dict[str, set[str]] = {}
    client_last_seen: dict[str, float] = {}
    client_last_ip: dict[str, str] = {}

    for row in db_state:
        name = row["client_name"]
        ws = row["workspace_id"]
        seen = row["last_seen"]
        ip = row.get("last_ip") or ""

        client_workspaces.setdefault(name, set()).add(ws)
        if name not in client_last_seen or seen > client_last_seen[name]:
            client_last_seen[name] = seen
            if ip:
                client_last_ip[name] = ip
        elif not client_last_ip.get(name) and ip:
            client_last_ip[name] = ip

    all_names = sorted(set(keys_clients.keys()) | set(client_workspaces.keys()))

    rows = []
    headers = ["Client", "Status", "Last Seen", "Last IP", "Active Workspaces"]
    for name in all_names:
        last_seen = client_last_seen.get(name)
        active = is_client_active(name)
        status_str = format_status(last_seen, active)
        seen_str = format_time(last_seen)
        ip_str = client_last_ip.get(name) or "-"
        ws_set = client_workspaces.get(name, set())
        ws_str = ", ".join(sorted(ws_set)) if ws_set else "-"

        rows.append([name, status_str, seen_str, ip_str, ws_str])

    print(format_table(headers, rows))
    return 0


def cmd_status() -> int:
    summaries = db.get_workspaces_summary()
    client_states = db.get_clients_state()

    ws_clients: dict[str, list[dict]] = {}
    for cs in client_states:
        ws_clients.setdefault(cs["workspace_id"], []).append(cs)

    headers = ["Workspace", "Rev", "Files", "Size", "Last Modified", "By Client", "Client Lag"]
    rows = []

    for ws in summaries:
        ws_id = ws["workspace_id"]
        cur_rev = ws["current_revision"]
        file_count = str(ws["file_count"])
        size_str = format_size(ws["total_size"])
        mod_str = format_time(ws["last_modified"])
        client_str = ws["last_client"] or "-"

        c_list = ws_clients.get(ws_id, [])
        if not c_list:
            lag_str = "-"
        else:
            all_synced = all(c["last_synced_revision"] >= cur_rev for c in c_list)
            if all_synced:
                lag_str = "all synced"
            else:
                parts = []
                for c in sorted(c_list, key=lambda x: x["client_name"]):
                    lag = cur_rev - c["last_synced_revision"]
                    if lag <= 0:
                        parts.append(f"{c['client_name']} (synced)")
                    else:
                        parts.append(f"{c['client_name']} (-{lag})")
                lag_str = ", ".join(parts)

        rows.append([ws_id, str(cur_rev), file_count, size_str, mod_str, client_str, lag_str])

    print(format_table(headers, rows))
    return 0


def cmd_client_status(client_name: str) -> int:
    keys_clients, db_state = load_all_clients()
    client_records = [r for r in db_state if r["client_name"] == client_name]

    if client_name not in keys_clients and not client_records:
        print(f"error: client '{client_name}' not found", file=sys.stderr)
        return 1

    created_date = keys_clients.get(client_name, {}).get("created", "-") or "-"

    last_seen = None
    last_ip = "-"
    if client_records:
        latest = max(client_records, key=lambda r: r["last_seen"])
        last_seen = latest["last_seen"]
        last_ip = latest.get("last_ip") or "-"

    status_str = format_status(last_seen, is_client_active(client_name))

    print("Client Details:")
    print(f"  Name:     {client_name}")
    print(f"  Created:  {created_date}")
    print(f"  Status:   {status_str}")
    print(f"  Last IP:  {last_ip}")
    print()

    print("Workspace Sync State:")
    sync_headers = ["Workspace", "Client Rev", "Server Rev", "Lag", "Last Synced"]
    sync_rows = []
    for r in sorted(client_records, key=lambda x: x["workspace_id"]):
        ws_id = r["workspace_id"]
        client_rev = r["last_synced_revision"]
        server_rev = db.get_revision(ws_id)
        lag = server_rev - client_rev
        lag_str = "synced" if lag <= 0 else f"-{lag}"
        synced_str = format_time(r["last_seen"])
        sync_rows.append([ws_id, str(client_rev), str(server_rev), lag_str, synced_str])
    print(format_table(sync_headers, sync_rows))
    print()

    print("Recent File Activity:")
    recent_headers = ["Workspace", "Rev", "File Path", "Size", "Timestamp", "Action"]
    recent_files = db.get_client_recent_files(client_name, limit=10)
    recent_rows = []
    for f in recent_files:
        act = "Deleted" if f["is_deleted"] else "Updated"
        recent_rows.append([
            f["workspace_id"],
            str(f["revision"]),
            f["path"],
            format_size(f["size"]),
            format_time(f["created_at"]),
            act,
        ])
    print(format_table(recent_headers, recent_rows))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="SyncMe Server Management CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("clients", help="List registered and connected clients")
    sub.add_parser("status", help="Show workspace overview and client lag")

    client_parser = sub.add_parser("client", help="Client specific operations")
    client_sub = client_parser.add_subparsers(dest="client_cmd", required=True)
    status_parser = client_sub.add_parser("status", help="Show sync state and recent activity for a client")
    status_parser.add_argument("name", help="Name of client")

    args = parser.parse_args(argv)
    if args.cmd == "clients":
        return cmd_clients()
    elif args.cmd == "status":
        return cmd_status()
    elif args.cmd == "client":
        if args.client_cmd == "status":
            return cmd_client_status(args.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
