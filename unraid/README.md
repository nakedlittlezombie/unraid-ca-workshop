# CA Workshop Unraid plugin

This folder contains the Unraid packaging for CA Workshop. The plugin installs the
existing local app under `/usr/local/emhttp/plugins/ca-workshop`, starts the Python
service on `127.0.0.1:8765`, and adds a **Utilities → CA Workshop** page.

Unraid's web server is the only thing exposed to the network. The browser page talks
to `unraid/api.php`, which proxies `/api/*` requests to the loopback-only Python
service. The service itself is never bound to a LAN address.

## Requirements

- Unraid 6.12 or later.
- Python 3 on the Unraid host. Install it with a package such as **NerdTools** if it
  is not already present. The plugin reports a clear error in `/var/log/ca-workshop.log`
  when `python3` is missing.
- Docker, which Unraid already provides.
- Python `PyYAML` on the host for Compose import. The rest of CA Workshop works
  without it; Compose conversion warns that PyYAML is unavailable.
- Optional: **Community Applications** installed on the same server.

## Build and Release

To build the plugin package, commit changes, push to GitHub, and create or update a GitHub release in one step:

```sh
# Auto-detects next version for today, builds, commits, pushes, and releases:
./scripts/release.sh

# Or with specific version and notes:
./scripts/release.sh --version 2026.10.30 --notes "Added new dashboard features"

# Or using GitHub's auto-generated commit notes:
./scripts/release.sh --generate-notes
```

To build locally without committing or releasing:

```sh
python3 scripts/build_unraid_plugin.py \
  --version 2026.10.29 \
  --plugin-url https://example.com/ca-workshop-2026.10.29.plg \
  --support-url https://example.com/ca-workshop \
  --output dist
```

The output is `dist/ca-workshop-<version>.plg` and `dist/ca-workshop.plg`. Host that file at the URL you passed
to `--plugin-url`, then install it from **Plugins → Install Plugin** in the Unraid
webGUI or with:

```sh
plugin install https://example.com/ca-workshop-2026.10.29.plg
```

The generated `.plg` embeds the app text files, so it does not need a separate release
archive. Rebuild it after changing `server.py`, `app.js`, `index.html`, `style.css`, or
the files in this folder.

## How it uses the Unraid host

When `/etc/unraid-version` or an installed Community Applications plugin is detected,
CA Workshop switches to Unraid host mode:

- **Docker** uses the host Docker socket (`/var/run/docker.sock`) and reports the
  Unraid Docker version in **Settings → Host runtime**. The local Docker sandbox menu item and view are omitted in Unraid plugin mode in favor of native Unraid Docker management. Container installs still use
  the app's loopback port publishing, resource limits, and volume safeguards.
- **Plugins** use Unraid's own
  `/usr/local/emhttp/plugins/dynamix.plugin.manager/scripts/plugin` command. CA Store
  plugin entries show **Install plugin** on Unraid and ask for confirmation before
  calling the plugin manager.
- **Community Applications** is detected at
  `/usr/local/emhttp/plugins/community.applications`. CA archive downloads and package update checks are omitted on Unraid; the CA Store reads the
  public catalog; plugin installation is handed to the Unraid plugin manager.
- **User templates** are detected at `/boot/config/plugins/dockerMan/templates-user`.
- **Dashboard widget** integrates directly into Unraid's main Dashboard page (`CAWorkshopDashboard.page`), displaying your pending approvals and approved apps as icons with status indicators below each icon. Can be toggled on/off in CA Workshop Settings.
- **Settings page** integrates under **Settings → User Utilities** (`CAWorkshopSettings.page`), allowing you to configure the Dashboard widget display and customize WebGUI navigation placement (Dedicated top-level menu tab, Tools/Utilities menu, or Hidden from main tabs).
- Plugin/state data persists in `/boot/config/plugins/ca-workshop` instead of the
  RAM-backed plugin directory.

`CA_WORKSHOP_UNRAID=1` can force Unraid mode for development. The app never trusts
that flag for security decisions; it still requires the real plugin manager and
Docker socket to exist before host actions run.

## Start, stop, and logs

```sh
/usr/local/emhttp/plugins/ca-workshop/rc.ca-workshop start
/usr/local/emhttp/plugins/ca-workshop/rc.ca-workshop stop
/usr/local/emhttp/plugins/ca-workshop/rc.ca-workshop status
tail -f /var/log/ca-workshop.log
```

The plugin starts the service after installation. On every boot Unraid reinstalls the
plugins stored in `/boot/config/plugins`, which reruns this install command and starts
the service again. If you stop the service manually, run the start command or reboot.

## Security notes

- The Python service binds only to `127.0.0.1`. Do not add a `--host` override that
  exposes it directly to the network.
- `unraid/api.php` only forwards paths that begin with `/api/` to the fixed loopback
  address. It does not accept arbitrary URLs, and it requires the
  `X-CA-Workshop: 1` header that the plugin page sends. Cross-site requests cannot set
  that header without a CORS preflight, which the proxy does not answer.
- Plugin install/remove endpoints require `confirmed: true`, are unavailable outside
  Unraid mode, and call Unraid's own plugin manager. Review the plugin URL before
  confirming.
- GitHub, AI, and Unraid credentials are stored under `/boot/config/plugins/ca-workshop`.
  CA Workshop asks the filesystem for owner-only permissions, but the Unraid flash drive
  is usually FAT32 and does not enforce POSIX modes. Protect the server and its webGUI
  instead of relying on file permissions, and remove saved credentials you no longer use.
  The flash drive is not encrypted by CA Workshop.
- The plugin page inherits the Unraid webGUI session. Treat access to the Unraid
  webGUI as access to CA Workshop.

## Uninstall

Remove the plugin from **Plugins → Installed Plugins**. The generated package includes a
removal hook that stops the service and deletes the installed files. You can also run it
manually; the installed plugin filename matches the `.plg` you installed, for example:

```sh
plugin remove ca-workshop-2026.10.29.plg
```

Saved data stays in `/boot/config/plugins/ca-workshop`. Remove that folder only if you
also want to delete the saved repository selection, activity, and credentials.
