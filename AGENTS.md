# CA Workshop agent guide

## Project

CA Workshop is a loopback-only web workbench for preparing Unraid Community Applications repositories, using the official submission portal, browsing CA listings, inspecting the Community Applications package, and testing supported Docker templates. Read `README.md` for the current user-facing feature set and startup instructions.

## Code map

- `server.py` serves the browser app and implements the local APIs, Compose YAML conversion, GitHub access, package archive updates, Docker adapter, and Unraid API connection.
- `index.html` defines the app screens and forms.
- `app.js` handles navigation, rendering, and browser-side API calls.
- `style.css` contains the CA-inspired theme and responsive layout.
- `fixtures/demo-repository/` is the bundled sample repository.
- `tests/` contains automated checks; inspect them before changing behavior they cover.

Keep each feature's behavior aligned across its server endpoint, UI, and README guidance. Keep dependencies small; `PyYAML` is used for safe Docker Compose parsing and is declared in `requirements.txt`.

## Safety and data

- Keep the web server bound to `127.0.0.1`; do not expose it to the network without a deliberate security design.
- Keep `.ca-workshop-state.json`, `.ca-workshop-unraid.json`, `.ca-workshop-package.json`, downloaded archives, and credentials out of Git. Never print or commit tokens or API keys.
- Download the CA package only from the official `unraid/community.applications` GitHub archive. Validate the expected filename, size, Git blob hash, and TXZ contents before activating a download. Do not execute or extract the package; the app previews selected files only.
- Treat repository templates and container images as third-party code. Preserve the Docker adapter's loopback-only port publishing, resource limits, isolated volumes by default, explicit bind mounts, and restrictions on privileged settings.
- Request only the Unraid API scopes needed for a feature. Keep CA portal sign-in separate from Unraid server authorization.
- Repository edits are local until committed and pushed. GitHub tokens must remain transient and must not be saved.

## Working approach

1. Inspect the relevant implementation and current Git status before editing. Preserve user changes and local ignored files.
2. Make focused changes in the existing structure. Avoid unrelated redesigns or adding dependencies without a clear need.
3. Update `README.md` when setup, behavior, data handling, permissions, or safety boundaries change.
4. Do not run automated tests unless the user asks to test. For code changes, syntax checks and reviewing the resulting diff are appropriate.
5. Report what changed, how it was checked, and any limitation that affects use. Commit or push only when the user has authorized publication.
