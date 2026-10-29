# CA Workshop

CA Workshop is a local workbench for maintaining an Unraid Community Applications repository through app creation, validation, submission tracking, and Docker testing. Its workspace separates repository setup, Docker applications, the CA Store, and task tools.

The app is intended to run on the same computer as the Docker engine used for testing. It does not replace the official CA submission and review process.

## What it can do

### Repository

- Connect an existing local clone of a Community Applications repository.
- Check repository metadata, license and template XML files with a local preflight.
- Validate Docker app configuration using the cross-platform `automation/template_validation/validate_app_config.py` checks, both during preflight and as a standalone command.
- Create a public repository from Unraid's [`unraid-community-apps-starter`](https://github.com/unraid/unraid-community-apps-starter) template with a GitHub token, clone it locally, and fill in the starter metadata.
- Check repository metadata, license, and public GitHub availability with a local preflight.
- Edit repository setup files and `ca_profile.xml`; application templates and icons are managed from **Applications**. Edits are saved to the local folder; use Git to review, commit, and push them.

### Applications

- View every Docker template with its own local validation results, schema status, and actionable **Fix** link. Repository readiness remains focused on repository setup.
- Import a Compose file, map each service into the bundled dummy XML starter, and continue in the graphical editor.
- Create a template without Compose using **Start from dummy XML**, edit template metadata and Docker configuration fields, preview the XML, and save under `templates/`.
- Manage app icons, including Font Awesome artwork and missing or placeholder icon checks.
- Use the Applications workspace subsections for the app list, template editor, submission tracker, and published-app statistics.
- Record an app's submission status in the local tracker after confirming it in the official portal. Status markers are stored in this browser for the connected repository; they are not read from or submitted to CA automatically.
- View CA store details and metrics for apps marked **Published** when a listing can be matched; values unavailable from a source are shown as unavailable.

Plugin management is reserved for a later phase after the Docker application workflow is complete.

### Tools and submission workflow

- Configure a GitHub personal access token in **Settings** and sync changed app templates, icons, and profile files to the current GitHub branch. The submission tracker blocks the **Submitted** status until the repository is public, active, and cleanly synced.
- Connect an Unraid server from **Settings**. Authorization uses the server's API key flow and its existing OIDC sign-in when configured.
- Read the [official submission help](https://ca.unraid.net/submit/help) and related repository-format documentation.
- Open the official submission portal within the Applications submission workspace, or open it in a separate browser tab if its sign-in or popup flow requires that.
- Run a local preflight before using the portal's official Validate, Scan, and Submit steps.

The official portal remains the authority for validation, submission status, and review. CA Workshop does not submit or change a listing on your behalf.

### Browse and test apps

- Search and filter the public Community Apps catalog.
- Inspect public Docker templates hosted on GitHub and install supported CA Store entries into the configured local Docker engine.
- Use isolated Docker-managed volumes for path settings by default. You can opt into validated host bind mounts when you need to test against a specific local folder.
- Publish app ports only on loopback and apply resource limits. Unsupported or privileged template settings are not passed through.
- Use **Tools → Docker sandbox** to view only running containers installed from the CA Store. Repository templates remain in the Applications editor; manual image runs are not included in this view.
- Use **Connect demo fixture** to load the bundled minimal Nginx example. Docker pulls its image only after you confirm installation.

The catalog browser uses public CA web endpoints that are not documented as a stable third-party API, so CA site changes may require updates to this connector. Plugin entries are linked back to CA because plugin installation requires an Unraid host.

### Inspect the supplied Community Applications package

The **CA archive** tool indexes and previews files from the Unraid Community Applications `.txz` package without running its PHP plugin code. On first use, CA Workshop checks the official [`unraid/community.applications` archive folder](https://github.com/unraid/community.applications/tree/master/archive) and downloads the latest dated x86_64 package automatically. Use **Check for updates** to compare the local copy with GitHub; if a newer build is available, choose **Download update**. Downloads are saved locally, checked against GitHub's published file hash and size, and validated as a TXZ archive before being activated. The archive is used as a reference for the CA Store's organization and workflow; actual listings and install templates are fetched from the live public CA catalog endpoints. CA Workshop previews selected archive files and does not execute or extract the package.

### Connect an Unraid server

From the Submissions page, authorize an Unraid server to inspect its system and existing Docker containers. The app requests `system:read`, `docker:read`, `docker:update`, and `docker:delete`. Depending on the server version and granted permissions, controls include starting, stopping, restarting, updating, and removing existing containers.

The server's authorization screen can use its configured sign-in, including Unraid.net OIDC. This connection is separate from Community Apps portal sign-in. The currently published Unraid GraphQL API schema has controls for existing containers but no container creation/install operation, so new templates are installed using the local Docker engine instead.

## Requirements

- Python 3.10 or later.
- A modern browser with JavaScript enabled.
- Docker CLI and a reachable Docker daemon for local template installation.
- Git for working with local repositories and GitHub clones.
- A GitHub account and personal access token for repository bootstrap and HTTPS publishing. SSH remotes can use the local SSH agent instead.
- An Unraid server only if you use server inspection and container controls.
- `xmllint` (optional; enables XSD checks when the connected repository includes `templates/template_schema.xsd`).
- An internet connection on first use and when checking for package updates.
- PyYAML, installed from `requirements.txt` for Compose YAML parsing.

## Start CA Workshop

From the project folder, run:

```sh
python3 -m pip install -r requirements.txt
python3 server.py
```

Then open [http://127.0.0.1:8765](http://127.0.0.1:8765). The server is configured for loopback access; do not expose it directly to an untrusted network.

To stop the app, interrupt the running process in the terminal where it was started.

## First use

1. Open **Repository** and connect an existing local clone, or create a new public repository from the starter template.
2. Complete repository setup and readiness checks. Use the guided starter app form when offered, or open **Applications** to import Compose or create from the dummy XML template.
3. Review and save the generated app template and icon, then run the local preflight and resolve any issues.
4. From **Applications**, open the submission workspace and follow the CA portal's Validate, Scan, and Submit workflow.
5. Use **CA Store** to browse and install supported Docker apps. Track only running CA Store installs in **Tools → Docker sandbox**.
6. Optionally authorize an Unraid server to inspect and control existing containers there.

### Template icons

The **Applications → Manage app icons** workspace adapts the icon workflows from [font_awesome_icon.sh](https://github.com/nwithan8/unraid_templates/blob/main/scripts/font_awesome_icon.sh), [populate_missing_icons.sh](https://github.com/nwithan8/unraid_templates/blob/main/scripts/populate_missing_icons.sh), [replace_all_placeholder_icons.sh](https://github.com/nwithan8/unraid_templates/blob/main/scripts/replace_all_placeholder_icons.sh), and [check_placeholder_icons.sh](https://github.com/nwithan8/unraid_templates/blob/main/scripts/check_placeholder_icons.sh). Font Awesome SVGs are rendered to PNG by the browser; missing icon files are copied with the app’s cross-platform file APIs; placeholder checks compare decoded pixels in the browser. The workflow does not call Bash, ImageMagick, or OS-specific `stat` commands.

### Template validation

Repository preflight runs the checks from the adapted [`validate_app_config.py`](automation/template_validation/validate_app_config.py), based on [nwithan8’s upstream validator](https://github.com/nwithan8/unraid_templates/blob/main/automation/template_validation/validate_app_config.py), for every Docker template. It checks the XML declaration, WebUI and Network format, a matching Port Config for the WebUI port (except host networking), and all-caps Config targets that should be Variables. If the repository contains `templates/template_schema.xsd`, preflight also validates templates against it using [`idddev/xml-xsd-validator`](https://github.com/idddev/xml-xsd-validator/tree/d676254dbfe82b80eab6548ab2e420d8f695652e)’s `xmllint` engine. XSD checks show as skipped when the schema or `xmllint` is unavailable. The app invokes `xmllint` with separate arguments rather than using the upstream Node wrapper’s shell command, so repository paths with spaces are handled safely. The app-specific validator also runs on its own with `python automation/template_validation/validate_app_config.py templates/your-template.xml`.

## Local data and credentials

The app stores its selected repository and local activity in `.ca-workshop-state.json` in the project folder. The Unraid API key is stored separately in `.ca-workshop-unraid.json` with owner-only file permissions. The downloaded package and its version record are also stored locally. These local files are ignored by Git. Disconnecting the server removes the saved API key file.

GitHub tokens are saved under `~/.config/ca-workshop/github.json` with owner-only file permissions, outside the connected repository. They are never shown again in the interface or included in Git history. Remove a token from **Settings** or revoke it through GitHub when it is no longer needed. Unraid API keys remain in a separate owner-only local settings file.

## Docker and template safety

Docker templates and their container images are third-party code. Review the publisher, image, paths, ports, and settings before installing. Installs are explicit and use the app's local Docker adapter. The adapter does not enable privileged mode, host networking, device access, or arbitrary Docker arguments. Bind mounts are opt-in for each path, protected system locations are rejected, and template read-only settings are retained.

The Unraid connection can update or remove existing containers when you grant the requested API scopes. Review each confirmation before approving an action. CA Workshop does not create new containers on Unraid through the currently published API schema.

## Troubleshooting

- **Docker unavailable:** Start the Docker daemon and confirm the Docker CLI can reach it from the same environment running CA Workshop.
- **Template cannot be installed:** Check that it is a supported Docker template with an accessible image and supported settings. Review the on-screen install feedback for the failing step.
- **CA portal sign-in does not work in the embedded view:** Choose **Open in new tab** and sign in there.
- **Unraid connection fails:** Confirm the host address is reachable from this computer, restart authorization from the app, and grant the requested scopes on the Unraid server.
- **Repository files do not appear:** Connect the local clone's root folder and make sure the repository files are present. Browser editor changes are saved to that local folder, not pushed to GitHub automatically.
- **Guided template setup is not available:** The repository needs a valid root profile and license and must be public and active on GitHub first. After saving a template, CA Workshop asks whether to run preflight.
- **CA archive is empty:** Wait for the first download to finish. If it fails, check your internet connection and choose **Check for updates** to retry.

## Project layout

```text
.
├── app.js                 # Browser interface behavior
├── index.html             # App pages and forms
├── style.css              # CA-inspired theme and layout
├── server.py              # Local HTTP service and adapters
├── fixtures/              # Demo repository template
└── tests/                 # Automated checks
```

## Status

CA Workshop is an early local development tool. Catalog integration depends on CA's public site behavior; official submission actions happen on the CA portal; and remote installation to Unraid is not available through the currently published GraphQL schema. Contributions and issue reports are welcome.
