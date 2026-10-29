# Unraid API Capabilities — Primary Source Research

Source: https://docs.unraid.net/API/ (primary docs), subpages fetched directly.

## Availability
- Starting Unraid 7.2: native integration, no plugin required. Auto-starts; settings at `Settings → Management Access → API`.
- Pre-7.2 / newer features: install `Unraid Connect` plugin from Community Applications.
- Source claims from docs.unraid.net `/API/` page.

## Authentication
- API Keys (programmatic access)
- Cookies (automatic when signed in to WebGUI)
- SSO / OIDC (external providers configured via `Settings → Management Access → API → OIDC`)
- Source: `/API/how-to-use-the-api/` section `authentication`.

## GraphQL Interface
- Query language runtime; modern strongly-typed API.
- Multiple auth methods noted above.
- Source: `/API/` overview text; `/API/how-to-use-the-api/`.

## CLI Commands (`unraid-api`)
Source: `/API/cli/`
- `start [--log-level <level>]` (levels: trace/debug/info/warn/error/fatal)
- `stop [--delete]`
- `restart [--log-level <level>]`
- `logs [-l <lines>]` (default 100)
- `config`
- `switch-env [-e <production|staging>]`
- `developer` (GraphQL Sandbox enable/disable; Modal Testing Tool; also accessible in WebGUI at `Settings → Management Access → Developer Options`)
- `apikey [--name <name>] [--create] [-r <roles>] [-p <permissions>] [-d <description>]`
- `sso` (add-user/remove-user/remove; list-users/list; validate-token `<token>`)
- `report [-r] [-j]`

## API Key / Scope Format
Source: `/API/api-key-app-developer-authorization-flow/`
- Format: `resource:action`
- Resources mentioned: `docker`, `vm`, `system`, `share`, `user`, `network`, `disk`
- Actions: `create`, `read`, `update`, `delete`, `*`
- Example scope strings visible: `docker:read`, `vm:*`, `system:update`, `role:viewer`, `role:admin`

## Schemas (GraphQL)
Source: `/API/how-to-use-the-api/` section `available-schemas`
- `system`, `array`, `docker`, `remote-access`
- Roles mentioned: `admin`, `connect`, `guest`

## Example Queries
Source: `/API/how-to-use-the-api/` section `example-queries`
- `info`
- `array`
- `dockerContainers`

## Rate Limiting / Error Handling
Source: `/API/how-to-use-the-api/` section `error-handling-and-rate-limiting`
- Rate limits exist; Apollo Studio mentioned for development.
- Best practices noted; exact limits not fully expanded in fetched HTML.

## SSO / OIDC Setup
Source: `/API/oidc-provider-setup/`
- Redirect URI: `http://YOUR_UNRAID_IP/graphql/api/auth/oidc/callback`
- WebGUI access: `Settings → Management Access → API → OIDC`

## Safety / Permissions Notes
- Most commands require appropriate permissions.
- Some commands require the API to be running or stopped.
- Store API keys securely; they provide system access.
- SSO configuration changes may require service restart.
- Source: `/API/cli/` end notes.

## File Location
Saved to: `/home/naked/dev/CA Workshop/research/unraid-api-capabilities.md`
No pre-existing `research/` or `notes/` convention existed; file placed at repo root under `research/` and path reported above.
