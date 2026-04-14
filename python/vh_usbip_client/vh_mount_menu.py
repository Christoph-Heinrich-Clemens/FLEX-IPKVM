#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import List, Optional


DEFAULT_WINDOWS_CANDIDATES = [
    r"C:\Program Files\VirtualHere\vhui64.exe",
    r"C:\Program Files\VirtualHere\vhui.exe",
    r"C:\vhui64.exe",
    r"C:\vhui.exe",
]

DEFAULT_LINUX_CANDIDATES = [
    "/opt/virtualhere/vhclientx86_64",
    "/opt/virtualhere/vhclientarm64",
    "/opt/virtualhere/vhclientarm",
    "/usr/local/bin/vhclientx86_64",
    "/usr/local/bin/vhclientarm64",
    "/usr/local/bin/vhclientarm",
]

HUB_LINE_RE = re.compile(r"^\s*(?P<hub>.+?)\s+\((?P<endpoint>[^()]*:[^()]*)\)\s*$")
DEVICE_LINE_RE = re.compile(
    r"^\s*-->\s+(?P<auto_use>\*\s+)?(?P<name>.+?)\s+\((?P<address>[^()]+)\)(?:\s+\((?P<status>[^()]+)\))?\s*$"
)
COMMAND_SETTLE_DELAY_SECONDS = 1.0


@dataclass
class Device:
    hub_name: str
    name: str
    address: str
    status: str = ""
    auto_use: bool = False

    @property
    def label(self) -> str:
        return f"{self.hub_name} -> {self.name}"

    @property
    def is_in_use(self) -> bool:
        return self.status.strip().lower().startswith("in-use")

    @property
    def in_use_by_you(self) -> bool:
        return self.status.strip().lower() == "in-use by you"

    @property
    def can_use(self) -> bool:
        return not self.is_in_use


class VirtualHereError(RuntimeError):
    pass


class VirtualHereClient:
    def __init__(self, executable: str):
        self.executable = executable

    def run(self, command: str) -> str:
        if os.name == "nt":
            return self._run_windows(command)
        return self._run_posix(command)

    def _run_posix(self, command: str) -> str:
        try:
            proc = subprocess.run(
                [self.executable, "-t", command],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise VirtualHereError(f"VirtualHere client not found: {self.executable}") from exc
        except OSError as exc:
            raise VirtualHereError(f"Failed to run VirtualHere client: {exc}") from exc

        output = (proc.stdout or "") + (proc.stderr or "")
        output = output.strip()

        if not output:
            raise VirtualHereError(
                "VirtualHere client returned no output. Ensure the client is installed and already running."
            )
        return output

    def _run_windows(self, command: str) -> str:
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
                tmp_name = tmp.name

            proc = subprocess.run(
                [self.executable, "-t", command, "-r", tmp_name],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise VirtualHereError(f"VirtualHere client not found: {self.executable}") from exc
        except OSError as exc:
            raise VirtualHereError(f"Failed to run VirtualHere client: {exc}") from exc

        file_output = ""
        if tmp_name and os.path.exists(tmp_name):
            try:
                with open(tmp_name, "r", encoding="utf-8", errors="replace") as f:
                    file_output = f.read()
            finally:
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass

        output = file_output.strip()
        if not output:
            output = ((proc.stdout or "") + (proc.stderr or "")).strip()

        if not output:
            raise VirtualHereError(
                "VirtualHere returned no text result. On Windows the client must already be running, and the script uses '-r' because vhui64.exe is a GUI program."
            )
        if output.upper().startswith("IPC ERROR"):
            raise VirtualHereError(
                "VirtualHere IPC is unavailable. Ensure the client is already running before using this tool."
            )
        return output

    def list_devices(self) -> List[Device]:
        raw = self.run("LIST")
        if raw.startswith("ERROR:"):
            raise VirtualHereError(raw)
        return parse_list_output(raw)

    def use_device(self, address: str) -> str:
        return self._run_mount_command(
            command=f"USE,{address}",
            address=address,
            expect_in_use=True,
            success_message="OK (VirtualHere timed out, but the device is now mounted.)",
        )

    def stop_using(self, address: str) -> str:
        return self._run_mount_command(
            command=f"STOP USING,{address}",
            address=address,
            expect_in_use=False,
            success_message="OK (VirtualHere timed out, but the device is now unmounted.)",
        )

    def _run_mount_command(
        self,
        command: str,
        address: str,
        expect_in_use: bool,
        success_message: str,
    ) -> str:
        output = self.run(command)
        status_line = output.strip().splitlines()[0].strip().upper()

        if status_line.startswith("ERROR:"):
            raise VirtualHereError(output)

        if status_line == "FAILED" and self._device_state_matches(address, expect_in_use):
            return success_message

        return output

    def _device_state_matches(self, address: str, expect_in_use: bool) -> bool:
        time.sleep(COMMAND_SETTLE_DELAY_SECONDS)
        try:
            devices = self.list_devices()
        except VirtualHereError:
            return False

        device = next((item for item in devices if item.address == address), None)
        if expect_in_use:
            return bool(device and device.in_use_by_you)
        return device is None or not device.in_use_by_you



def pick_default_executable() -> Optional[str]:
    env_value = os.environ.get("VHCLIENT")
    if env_value:
        return env_value

    candidates = DEFAULT_WINDOWS_CANDIDATES if os.name == "nt" else DEFAULT_LINUX_CANDIDATES

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    for name in ("vhui64.exe", "vhui.exe", "vhclientx86_64", "vhclientarm64", "vhclientarm"):
        path = shutil.which(name)
        if path:
            return path

    return None



def parse_list_output(raw: str) -> List[Device]:
    devices: List[Device] = []
    current_hub = ""

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        hub_match = HUB_LINE_RE.match(line)
        if "-->" not in line and hub_match:
            current_hub = hub_match.group("hub").strip()
            continue

        if "-->" not in line:
            continue

        try:
            device = parse_device_line(line=line, current_hub=current_hub)
        except ValueError:
            continue
        devices.append(device)

    return devices



def parse_device_line(line: str, current_hub: str) -> Device:
    match = DEVICE_LINE_RE.match(line)
    if not match:
        raise ValueError(f"Unrecognized device line: {line}")

    return Device(
        hub_name=current_hub or "Unknown Hub",
        name=match.group("name").strip(),
        address=match.group("address").strip(),
        status=(match.group("status") or "").strip(),
        auto_use=bool(match.group("auto_use")),
    )



def print_devices(devices: List[Device], title: str) -> bool:
    print()
    print(title)
    if not devices:
        print("  none")
        print()
        return False

    for idx, device in enumerate(devices, start=1):
        suffix_parts = []
        if device.auto_use:
            suffix_parts.append("Auto-use")
        if device.status:
            suffix_parts.append(device.status)
        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
        print(f"  [{idx}] {device.label} [{device.address}]{suffix}")
    print()
    return True



def select_device(devices: List[Device]) -> Device:
    choice = input("Choose device number: ").strip()
    if not choice.isdigit():
        raise ValueError("Invalid device number.")

    index = int(choice)
    if index < 1 or index > len(devices):
        raise ValueError("Selection out of range.")

    return devices[index - 1]



def interactive_menu(client: VirtualHereClient) -> int:
    while True:
        try:
            devices = client.list_devices()
        except VirtualHereError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        available_devices = [device for device in devices if device.can_use]
        used_devices = [device for device in devices if device.in_use_by_you]

        print_devices(devices, "Available VirtualHere devices:")
        print("[A]dd  [R]emove  [L]ist  [E]xit")
        print()

        action = input("Choose action: ").strip().upper()

        if action == "A":
            if not available_devices:
                print("No devices available.\n")
                continue
            print_devices(available_devices, "Devices available to mount:")
            try:
                picked = select_device(available_devices)
            except ValueError as exc:
                print(f"{exc}\n")
                continue

            print()
            print(f"Adding: {picked.label} [{picked.address}]")
            print(client.use_device(picked.address))
            print()
        elif action == "R":
            if not print_devices(used_devices, "Devices currently in use by this client:"):
                print("Nothing to remove.\n")
                continue
            try:
                picked = select_device(used_devices)
            except ValueError as exc:
                print(f"{exc}\n")
                continue

            print()
            print(f"Removing: {picked.label} [{picked.address}]")
            print(client.stop_using(picked.address))
            print()
        elif action == "L":
            continue
        elif action == "E":
            return 0
        else:
            print("Invalid action.\n")



def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cross-platform VirtualHere mount menu for Windows and Linux."
    )
    parser.add_argument(
        "--vhclient",
        default=pick_default_executable(),
        help="Path to the VirtualHere client executable. Can also be set via VHCLIENT.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List devices once and exit.",
    )
    parser.add_argument(
        "--use",
        metavar="ADDRESS",
        help="Use a device by VirtualHere address, e.g. GL-BE3600.1134",
    )
    parser.add_argument(
        "--stop-using",
        metavar="ADDRESS",
        help="Stop using a device by VirtualHere address.",
    )
    args = parser.parse_args(argv)

    if not args.vhclient:
        system_name = platform.system()
        print(
            f"VirtualHere client executable not found automatically on {system_name}.\n"
            "Pass --vhclient /path/to/client or set the VHCLIENT environment variable.",
            file=sys.stderr,
        )
        return 1

    client = VirtualHereClient(args.vhclient)

    try:
        if args.list:
            devices = client.list_devices()
            print_devices(devices, "Available VirtualHere devices:")
            return 0
        if args.use:
            print(client.use_device(args.use))
            return 0
        if args.stop_using:
            print(client.stop_using(args.stop_using))
            return 0
        return interactive_menu(client)
    except VirtualHereError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
