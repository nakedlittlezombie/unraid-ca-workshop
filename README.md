# CA Workshop

CA Workshop is a local workbench for preparing Unraid Community Applications submissions and testing Docker templates before you submit them. It brings repository editing, submission guidance, the official Community Apps portal, catalog browsing, and a local Docker sandbox into one browser interface.

The app is intended to run on the same computer as the Docker engine used for testing. It does not replace the official CA submission and review process.

## What it can do

### Prepare a repository

- Connect an existing local clone of a Community Applications repository.
- Check repository metadata, license and template XML files with a local preflight.
- Browse, create, edit, and delete repository files in the browser. Edits are saved to the local folder; use Git to review, commit, and push them.
- Create a public repository from Unraid's [`unraid-community-apps-starter`](https://github.com/unraid/unraid-community-apps-starter) template with a GitHub token, clone it locally, and fill in the starter metadata.
- Review recent actions recorded by this local app.

### Submit through Community Apps

- Read the [official submission help](https://ca.unraid.net/submit/help) and related repository-format documentation.
- Open the official submission portal within the Submissions page, or open it in a separate browser tab if its sign-in or popup flow requires that.
- Run a local preflight before using the portal's official Validate, Scan, and Submit steps.

The official portal remains the authority for validation, submission status, and review. CA Workshop does not submit or change a listing on your behalf.

### Browse and test apps

- Search and filter the public Community Apps catalog.
- Inspect public Docker templates hosted on GitHub and install supported templates into the configured local Docker engine.
- Use isolated Docker-managed volumes for path settings by default. You can opt into validated host bind mounts when you need to test against a specific local folder.
- Publish app ports only on loopback and apply resource limits. Unsupported or privileged template settings are not passed through.
- Start and stop containers created by the local installer, and remove them from the sandbox.
- Use **Connect demo fixture** to load the bundled minimal Nginx example. Docker pulls its image only after you confirm installation.

The catalog browser uses public CA web endpoints that are not documented as a stable third-party API, so CA site changes may require updates to this connector. Plugin entries are linked back to CA because plugin installation requires an Unraid host.

### Inspect the supplied Community Applications package

The Package sandbox indexes and previews files from the Unraid Community Applications `.txz` package without running its PHP plugin code. The package is intentionally not included in this Git repository. To enable this view, copy the package into the project folder using the expected filename:

```text
community.applications-2026.07.21-x86_64-1.txz
```

The archive is read-only in the app. It is optional for repository editing, catalog browsing, and Docker template testing.

### Connect an Unraid server

From the Submissions page, authorize an Unraid server to inspect its system and existing Docker containers. The app requests `system:read`, `docker:read`, `docker:update`, and `docker:delete`. Depending on the server version and granted permissions, controls include starting, stopping, restarting, updating, and removing existing containers.

The server's authorization screen can use its configured sign-in, including Unraid.net OIDC. This connection is separate from Community Apps portal sign-in. The currently published Unraid GraphQL API schema has controls for existing containers but no container creation/install operation, so new templates are installed using the local Docker engine instead.

## Requirements

- Python 3.10 or later; no third-party Python packages are required.
- A modern browser with JavaScript enabled.
- Docker CLI and a reachable Docker daemon for local template installation.
- Git for working with local repositories and GitHub clones.
- A GitHub account and personal access token only if you use the repository bootstrap feature.
- An Unraid server only if you use server inspection and container controls.
- The optional `.txz` package described above for Package sandbox browsing.

## Start CA Workshop

From the project folder, run:

```sh
python3 server.py
```

Then open [http://127.0.0.1:8765](http://127.0.0.1:8765). The server is configured for loopback access; do not expose it directly to an untrusted network.

To stop the app, interrupt the running process in the terminal where it was started.

## First use

1. Open **Repository** and connect an existing local clone, or create a new public repository from the starter template.
2. Review the starter files and edit the repository metadata and templates as needed. Changes are local until you commit and push them with Git.
3. Run the local preflight and resolve issues it finds.
4. Use **Browse CA** or **Package sandbox** to inspect apps and exercise supported Docker templates locally.
5. Use **Submissions** to open the official CA portal and follow its Validate, Scan, and Submit workflow.
6. Optionally authorize an Unraid server to inspect and control existing containers there.

## Local data and credentials

The app stores its selected repository and local activity in `.ca-workshop-state.json` in the project folder. The Unraid API key is stored separately in `.ca-workshop-unraid.json` with owner-only file permissions. Both files are ignored by Git. Disconnecting the server removes the saved API key file.

The GitHub token used for repository creation is sent to GitHub for that request and is not saved by CA Workshop. Review the requested permissions before providing a token, and revoke it through GitHub when it is no longer needed.

## Docker and template safety

Docker templates and their container images are third-party code. Review the publisher, image, paths, ports, and settings before installing. Installs are explicit and use the app's local Docker adapter. The adapter does not enable privileged mode, host networking, device access, or arbitrary Docker arguments. Bind mounts are opt-in for each path, protected system locations are rejected, and template read-only settings are retained.

The Unraid connection can update or remove existing containers when you grant the requested API scopes. Review each confirmation before approving an action. CA Workshop does not create new containers on Unraid through the currently published API schema.

## Troubleshooting

- **Docker unavailable:** Start the Docker daemon and confirm the Docker CLI can reach it from the same environment running CA Workshop.
- **Template cannot be installed:** Check that it is a supported Docker template with an accessible image and supported settings. Review the on-screen install feedback for the failing step.
- **CA portal sign-in does not work in the embedded view:** Choose **Open in new tab** and sign in there.
- **Unraid connection fails:** Confirm the host address is reachable from this computer, restart authorization from the app, and grant the requested scopes on the Unraid server.
- **Repository files do not appear:** Connect the local clone's root folder and make sure the repository files are present. Browser editor changes are saved to that local folder, not pushed to GitHub automatically.
- **Package sandbox is empty:** Place the optional `.txz` archive in the project folder with the exact filename shown above.

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
