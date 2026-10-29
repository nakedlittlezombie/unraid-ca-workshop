#!/usr/bin/env python3
"""Build a self-contained Unraid .plg for CA Workshop.

The generated plugin writes the app files under
/usr/local/emhttp/plugins/ca-workshop and runs the Python service on
127.0.0.1 only. Unraid's own web server reaches it through unraid/api.php.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
from pathlib import Path
from xml.sax.saxutils import escape

BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp"}

ROOT = Path(__file__).resolve().parent.parent
UNRAID = ROOT / "unraid"
DIST = ROOT / "dist"

APP_FILES = [
    "server.py",
    "app.js",
    "style.css",
    "requirements.txt",
    "images/ca-workshop-icon.png",
    "automation/template_validation/__init__.py",
    "automation/template_validation/validate_app_config.py",
]


def plugin_index_html() -> str:
    source = (ROOT / "index.html").read_text(encoding="utf-8")
    bootstrap = (
        "<script>\n"
        "window.CA_WORKSHOP_API_BASE='api.php?path=';\n"
        "try {\n"
        "  var t = new URLSearchParams(window.location.search).get('csrf_token');\n"
        "  if (t) window.csrf_token = t;\n"
        "  else if (window.parent && window.parent.csrf_token) window.csrf_token = window.parent.csrf_token;\n"
        "} catch(e) {}\n"
        "</script>\n"
    )
    marker = '<script src="app.js"></script>'
    if marker not in source:
        raise SystemExit("index.html no longer references app.js; update the plugin bootstrap injection.")
    return source.replace(marker, bootstrap + marker, 1)


def inline_file(path: str, content: str) -> str:
    return f'<FILE Name="{escape(path, {chr(34): "&quot;"})}">\n<INLINE>{escape(content)}</INLINE>\n</FILE>\n'


def inline_binary(path: str, data: bytes) -> str:
    """Emit a <FILE Run="/bin/bash"> block that base64-decodes the file on install."""
    b64 = base64.b64encode(data).decode()
    script = (
        f"mkdir -p \"$(dirname '{path}')\" && "
        f"printf '%s' '{b64}' | base64 -d > '{path}'"
    )
    return f'<FILE Run="/bin/bash">\n<INLINE>{escape(script)}</INLINE>\n</FILE>\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=dt.date.today().strftime("%Y.%m.%d"), help="Plugin version, for example 2026.10.29")
    parser.add_argument("--plugin-url", default="", help="Public URL of the generated .plg file")
    parser.add_argument("--support-url", default="", help="Support thread or repository URL")
    parser.add_argument("--author", default="CA Workshop contributors")
    parser.add_argument("--output", type=Path, default=DIST)
    args = parser.parse_args()

    if not args.plugin_url and not args.support_url:
        parser.error("provide --plugin-url and/or --support-url so Unraid can show plugin details")
    if args.output.exists() and not args.output.is_dir():
        raise SystemExit("Output path must be a directory.")

    parts: list[str] = []
    for relative in APP_FILES:
        source = ROOT / relative
        if not source.is_file():
            raise SystemExit(f"Missing build input: {relative}")
        dest = f"/usr/local/emhttp/plugins/ca-workshop/{relative}"
        if source.suffix.lower() in BINARY_EXTENSIONS:
            parts.append(inline_binary(dest, source.read_bytes()))
        else:
            parts.append(inline_file(dest, source.read_text(encoding="utf-8")))

    parts.append(inline_file("/usr/local/emhttp/plugins/ca-workshop/index.html", plugin_index_html()))
    parts.append(inline_file("/etc/rc.d/rc.ca-workshop", "#!/bin/bash\nexec /usr/local/emhttp/plugins/ca-workshop/rc.ca-workshop \"$@\"\n"))
    for relative, target in (
        ("api.php", "/usr/local/emhttp/plugins/ca-workshop/api.php"),
        ("rc.ca-workshop", "/usr/local/emhttp/plugins/ca-workshop/rc.ca-workshop"),
        ("CAWorkshop.page", "/usr/local/emhttp/plugins/ca-workshop/CAWorkshop.page"),
        ("CAWorkshopDashboard.page", "/usr/local/emhttp/plugins/ca-workshop/CAWorkshopDashboard.page"),
        ("CAWorkshopSettings.page", "/usr/local/emhttp/plugins/ca-workshop/CAWorkshopSettings.page"),
    ):
        parts.append(inline_file(target, (UNRAID / relative).read_text(encoding="utf-8")))

    attributes = [
        'name="ca-workshop"',
        f'author="{escape(args.author, {chr(34): "&quot;"})}"',
        f'version="{escape(args.version, {chr(34): "&quot;"})}"',
        'launch="Settings/CAWorkshopSettings"',
        'icon="/usr/local/emhttp/plugins/ca-workshop/images/ca-workshop-icon.png"',
    ]
    if args.plugin_url:
        attributes.append(f'pluginURL="{escape(args.plugin_url, {chr(34): "&quot;"})}"')
    if args.support_url:
        attributes.append(f'support="{escape(args.support_url, {chr(34): "&quot;"})}"')

    run = (
        "/bin/bash -c 'mkdir -p /boot/config/plugins/ca-workshop && "
        "chmod +x /etc/rc.d/rc.ca-workshop /usr/local/emhttp/plugins/ca-workshop/rc.ca-workshop && "
        "/etc/rc.d/rc.ca-workshop restart'"
    )
    changes = (
        f"CA Workshop {args.version}. Local Community Applications maintainer workspace. "
        "Runs a loopback-only service and uses the Unraid Docker socket and plugin manager when installed on Unraid."
    )
    removal = (
        "#!/bin/bash\n"
        "/etc/rc.d/rc.ca-workshop stop 2>/dev/null\n"
        "rm -f /etc/rc.d/rc.ca-workshop\n"
        "rm -rf /usr/local/emhttp/plugins/ca-workshop\n"
    )
    plg = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<PLUGIN {' '.join(attributes)}>\n"
        f"<CHANGES>{escape(changes)}</CHANGES>\n"
        + "".join(parts)
        + '<FILE Run="/bin/bash" Method="remove">\n'
        + f"<INLINE>{escape(removal)}</INLINE>\n</FILE>\n"
        + f"<RUN>{escape(run)}</RUN>\n"
        + "</PLUGIN>\n"
    )

    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / f"ca-workshop-{args.version}.plg"
    target.write_text(plg, encoding="utf-8")
    canonical = args.output / "ca-workshop.plg"
    canonical.write_text(plg, encoding="utf-8")
    print(f"Wrote {target} and {canonical} ({target.stat().st_size:,} bytes)")
    print("Install it with Unraid → Plugins → Install Plugin, or: plugin install <url-to-plg>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
