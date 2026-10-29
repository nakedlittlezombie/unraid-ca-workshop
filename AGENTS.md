# CA Workshop agent guide

## Project

CA Workshop is a loopback-only workbench that helps maintain a Community Applications repository through the full app lifecycle: create or import apps, validate repository files and templates, track CA submissions and approvals, and maintain published apps. Read `README.md` for the current user-facing feature set and startup instructions.

Keep the central workflow focused on helping a maintainer:

1. Add apps by editing a template manually, converting a Compose file, or pointing to a repository so the app can find a Compose file and generate a template.
2. Populate the required repository files, validate the app and repository, and guide the maintainer to the CA submission process.
3. Track each app from submission through review, approval, and publication. Treat the official CA portal as authoritative unless an official integration supplies status; never infer approval.
4. Manage published apps and show available store details such as application type, categories, added and updated dates, GitHub stars, CA downloads, image/repository, and Docker Hub stars. Preserve the source and meaning of each metric, and show unavailable values as unavailable rather than zero.

The `.unraid/community-apps-claim.json` file is repository claim metadata. Treat its nonce and public claim code as user-provided values; do not invent them.

The workspace sidebar separates Repository setup, Docker Applications, Plugins, shared Submissions, CA Store, and Tools. Keep Docker template creation/editing, Compose conversion, per-app validation results, app icons, and app statistics in Applications. Keep plugin listings and plugin statistics in Plugins; plugin editing and installation remain deferred. Use the single Submissions page to track Docker apps and plugins together. The Repository file browser and readiness checks are for repository setup and metadata; exclude app-specific template errors and app icons there. Submission markers are local user-entered tracking data; never present them as portal-confirmed status.

The CA Store browser loads paginated catalog results as the user scrolls. Docker install settings belong in its right-side drawer. Common install variables are saved server-side in `.ca-workshop-state.json` via `/api/settings/vars` and fill matching Docker environment variables by exact name; treat these values as user-local data and do not describe them as encrypted or sync them to GitHub.

## Code map

- `server.py` serves the browser app and implements the local APIs, Compose YAML conversion, GitHub access, package archive updates, Docker adapter, and Unraid API connection.
- `index.html` defines the app screens and forms.
- `app.js` handles navigation, rendering, and browser-side API calls.
- `style.css` contains the CA-inspired theme and responsive layout.
- Template editing uses the browser XML DOM to update values while retaining unedited elements; preserve this when changing the editor.
- `fixtures/demo-repository/` is the bundled sample repository.
- `tests/` contains automated checks; inspect them before changing behavior they cover.

Keep each feature's behavior aligned across its server endpoint, UI, and README guidance. Keep dependencies small; `PyYAML` is used for safe Docker Compose parsing and is declared in `requirements.txt`.

## Safety and data

- Keep the web server bound to `127.0.0.1`; do not expose it to the network without a deliberate security design.
- Keep `.ca-workshop-state.json`, `.ca-workshop-unraid.json`, `.ca-workshop-package.json`, downloaded archives, and credentials out of Git. Never print or commit tokens or API keys. GitHub credentials may be persisted only in `~/.config/ca-workshop/github.json` with owner-only permissions; Unraid credentials remain in the existing owner-only local secret file.
- Download the CA package only from the official `unraid/community.applications` GitHub archive. Validate the expected filename, size, Git blob hash, and TXZ contents before activating a download. Do not execute or extract the package; the app previews selected files only.
- Treat repository templates and container images as third-party code. Preserve the Docker adapter's loopback-only port publishing, resource limits, isolated volumes by default, explicit bind mounts, and restrictions on privileged settings.
- Request only the Unraid API scopes needed for a feature. Keep CA portal sign-in separate from Unraid server authorization.
- Repository edits are local until committed and pushed. The Settings page stores a verified GitHub token securely for GitHub pushes; sync only stages supported submission files and never force-pushes. Require relevant submission files to be synced to a public, active GitHub repository before marking an item submitted.

## Working approach

1. Inspect the relevant implementation and current Git status before editing. Preserve user changes and local ignored files.
2. Make focused changes in the existing structure. Avoid unrelated redesigns or adding dependencies without a clear need.
3. Update `README.md` when setup, behavior, data handling, permissions, or safety boundaries change.
4. Do not run automated tests unless the user asks to test. For code changes, syntax checks and reviewing the resulting diff are appropriate.
5. Report what changed, how it was checked, and any limitation that affects use. Commit or push only when the user has authorized publication.
