#!/usr/bin/env python3
"""Validate Unraid Docker app template configuration, with no OS-specific tools."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

WEBUI_PORT = re.compile(r"^https?://\[IP\]:\[PORT:(\d+)\]", re.IGNORECASE)


def _target_contains_port(target: str, port: int) -> bool:
    """Match a single target or a numeric target range without substring matches."""
    value = target.strip()
    if re.fullmatch(r"\d+", value):
        return int(value) == port
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", value)
    return bool(match and int(match.group(1)) <= port <= int(match.group(2)))


def validate_template_content(content: str) -> list[str]:
    """Return validation errors for a Docker template XML document."""
    errors: list[str] = []
    content = content.strip()
    if not content.startswith("<?xml"):
        errors.append("The XML declaration is missing.")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        return errors + [f"Malformed XML: {exc}"]

    webui = root.findtext("WebUI")
    if webui is not None:
        network = root.findtext("Network")
        if network is None or not network.strip():
            errors.append("WebUI is present but <Network> is missing.")
        elif network.strip().lower() != "host":
            match = WEBUI_PORT.match(webui.strip())
            if not match:
                errors.append("<WebUI> must use http(s)://[IP]:[PORT:<port_number>].")
            else:
                port = int(match.group(1))
                matching = any(
                    (config.get("Type") or "").strip().lower() == "port"
                    and _target_contains_port(config.get("Target") or "", port)
                    for config in root.findall("Config")
                )
                if not matching:
                    errors.append(f"No Port Config Target matches WebUI port {port}.")

    for config in root.findall("Config"):
        target = (config.get("Target") or "").strip()
        if target and target.isupper() and (config.get("Type") or "").strip().lower() != "variable":
            errors.append(f"Config Target '{target}' is all caps, so its Type should be Variable.")

    return errors


def validate_template_xsd(schema_path: str | Path, template_path: str | Path) -> dict[str, object]:
    """Validate a template with xmllint, the engine used by idddev/xml-xsd-validator.

    The upstream Node wrapper requires xmllint and constructs a shell command from
    file paths. Invoke the same engine with an argument list so paths containing
    spaces are safe and the call works on Windows, macOS, and Linux.
    """
    executable = shutil.which("xmllint")
    if not executable:
        return {"status": "skipped", "message": "xmllint is not installed; the XSD check was skipped."}
    try:
        result = subprocess.run(
            [executable, "--nonet", "--noout", "--schema", str(schema_path), str(template_path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "skipped", "message": "XSD validation timed out."}
    except OSError as exc:
        return {"status": "skipped", "message": f"Could not start xmllint: {exc}"}

    if result.returncode == 0:
        return {"status": "valid", "message": "Valid against template_schema.xsd."}
    output = (result.stderr or result.stdout).strip()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    messages = [line for line in lines if "fails to validate" not in line.lower()]
    if not messages:
        messages = ["Template does not match template_schema.xsd."]
    return {"status": "invalid", "message": " ".join(messages[:4])}


def validate_template_file(template_file_path: str | Path) -> bool:
    """Validate a template file and print any errors. Return True when valid."""
    path = Path(template_file_path)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error: Could not read {path}: {exc}")
        return False
    errors = validate_template_content(content)
    for error in errors:
        print(f"Error: {error}")
    if errors:
        print(f"{path} failed validation")
        return False
    print(f"{path} passed validation")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an Unraid Docker app template XML file.")
    parser.add_argument("template_file", help="Path to the app template XML file to validate.")
    args = parser.parse_args()
    return 0 if validate_template_file(args.template_file) else 1


if __name__ == "__main__":
    sys.exit(main())
