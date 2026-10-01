# Google Workspace

Google Workspace extension version 0.2.0 adds Gmail, Google Drive, and Google Sheets
to project workflows. OAuth configuration and every authorization are project-scoped.
`google_gmail_account`, `google_drive_account`, and `google_sheets_account` are
separate connections with separate tokens and grants. No default Google account exists:
every workflow operation requires an explicit compatible connection.

The runtime uses only the public `flowsteward_extension_sdk`. Gmail, Drive, and Sheets
live in separate extension-local packages so the bundle can move to its own repository
without importing Flow Steward Core implementation modules.

## Google Cloud and Flow Steward setup

1. Create or select one Google Cloud project. Configure its OAuth consent screen for
   the intended Internal or External audience and add test users where applicable.
2. Enable Gmail API, Google Drive API, Google Sheets API, and Google Picker API for
   the services you intend to offer.
3. Create one OAuth 2.0 Client ID of type **Web application**. Add the exact trusted
   Flow Steward callback URL as an authorized redirect URI. Add the exact trusted
   public HTTPS origin as an authorized JavaScript origin; only explicit loopback
   HTTP development is supported.
4. Create a browser API key restricted to Google Picker API and the exact authorized
   JavaScript origin. Copy the numeric Cloud project number as Picker App ID. The
   Picker API key is browser-visible non-secret configuration: restrict it in Google
   Cloud, but do not treat it as an encrypted credential.
5. On this extension's **Setup guide** tab for the selected project, save Client ID
   and Client Secret together. Picker API key, App ID, and browser origin are optional
   and needed only for the Drive and Sheets Picker shortcut.
6. A personal `gmail.com` owner can create this Web application client in their own
   Google Cloud project and enter its credentials in this extension Setup form. In a
   managed domain, review **Security → Access and data control → API controls** and
   allow the OAuth Client ID for the required organizational units when policy requires
   administrator approval.

Never put a Client Secret, access token, refresh token, authorization code, or OAuth
state in a manifest, workflow, log, issue, or support message. The browser-visible
Picker API key belongs only in the extension Setup form and must remain restricted to
its API and trusted origins.

### Verification and restricted data

`https://www.googleapis.com/auth/gmail.modify` is a Google Restricted scope. A
server-side deployment that stores or transmits restricted Gmail data may require
OAuth verification and a security assessment. Testing mode is limited to configured
test users and its grants expire; it is not a production deployment mode. Internal,
development, personal, and limited-use cases still have to follow Google's current
eligibility rules and the Google API Services User Data Policy.

New or reauthorized accounts request `openid` and Google's canonical
`https://www.googleapis.com/auth/userinfo.email` scope for stable Google subject
matching. Service access is incremental:

- Gmail uses only `https://www.googleapis.com/auth/gmail.modify`.
- Drive and Sheets share only `https://www.googleapis.com/auth/drive.file`.
- No broad Drive, `spreadsheets`, or combined browser scope is requested.

The extension declares three closed authorization intents: `gmail_connect`,
`drive_connect`, and `sheets_connect`. Each starts a separate consent flow with
`include_granted_scopes=false`; the UI never promises or requests a combined Gmail and
Drive consent. The frontend never submits raw scopes, endpoints, prompts, or login
hints. A reauthorization may preserve an existing refresh token when Google returns no
replacement. The browser Picker token exists only in memory and is never sent to the
extension runtime or persisted.

## Accounts and lifecycle

Personal Gmail and licensed Workspace mailboxes are supported subject to consent and
domain policy. An alias is the same primary mailbox; a Google Group without a usable
licensed Gmail mailbox is not a connectable account. Google subject is the durable
identity after identity scopes have been granted. Gmail, Drive, and Sheets remain
separate project-scoped connections and may resolve to the same Google identity;
another project manages its connections independently.

**Connect** starts authorization for the selected service type; the connection becomes
visible only after a successful code exchange. Reconnect reuses that connection and
uses `consent select_account`; a preserved email may be a hint but the user can choose
another account. Disconnect clears local access/refresh tokens, refs, OAuth states,
sessions, and runtime access while retaining recognizable email, subject, and service
choices. One account's lifecycle never changes another account.

A successful OAuth callback commits before the extension-owned post-commit identity
verification action runs through the normal runtime. Gmail, Drive, and Sheets connections can use
the same verified Google subject; individual resource access is checked by the relevant
runtime operation. A connected Gmail row exposes **Test** through the extension-owned
`test_connection` action; Drive and Sheets do not declare that Gmail-only control.
A temporary provider failure may leave a connected service degraded but usable;
disabled, disconnected, authorization-required, or unavailable services fail before
token refresh or network access.

Ordinary extension **Disable** preserves connections, grants, installation Client
credentials, and Picker credentials, while invalidating sessions and pending OAuth
flows. Confirmed **Uninstall** stops runtime access, removes local connection OAuth
state and secrets even if best-effort remote revoke fails, deletes the encrypted
Client Secret, clears browser-visible Picker configuration plus Client ID and Picker
App ID, and retains
recognizable orphan workflow definitions. It never deletes remote mail, files, or
spreadsheets.

## Workflow operations

Every step explicitly selects one compatible account. Picker is the interactive path
for files, folders, and spreadsheets; typed upstream IDs remain available for dynamic
workflows. Picker works with My Drive and Shared Drive resources visible to the
selected account and the `drive.file` grant.

Drive exposes exactly:

- `search_drive_files` — one bounded metadata page with a filter-bound opaque cursor;
- `download_drive_file` — streams a binary file or explicit supported native export
  into an artifact grant; and
- `upload_drive_file` — streams one owned artifact into a selected folder with
  idempotent create reconciliation.

Sheets exposes exactly:

- `read_google_sheet` — reads one exact sheet selector and relative A1 range into an
  NDJSON dataset grant;
- `write_google_sheet` — creates a spreadsheet or clears and overwrites one selected
  rectangle; and
- `append_google_sheet_rows` — appends after one selected table using a stable chunk
  marker.

Sheets writes use `RAW`, so formula-looking strings are stored as data rather than
being evaluated through `USER_ENTERED`. Reads cap preview at 100 rows. Full reads are
bounded by 100,000 rows, 1,000,000 cells, 200 MiB, and the smaller signed grant.
Drive file transfer defaults to 200 MiB and is bounded by the smaller signed artifact
grant. Operators may change the shared producer/consumer ceiling with
`FS_EXTENSION_ARTIFACT_MAX_BYTES`. Resumable Drive uploads use 8 MiB chunks and a
300-second default operation deadline; `FS_EXTENSION_OPERATION_TIMEOUT_SECONDS`
changes that deadline while the host retains bounded reconciliation headroom. Serialized
Sheets value batches are at most 2 MiB. File and dataset bodies never enter subprocess
JSON or workflow state.

Gmail behavior remains compatible: multiple mailbox connections are explicit, search
preserves provider order, cursor contents are never logged, labels retain Gmail
semantics, read state is not a processed marker, and decoded attachments remain
artifact-only with a 20 MiB policy ceiling.

## Deliberate non-goals

This release provides no generic Google API or raw HTTP operation, Drive sharing or
permission management, Sheets formatting, formula execution, Apps Script, hidden
history/snapshot behavior, or generic file conversion. CSV/XLSX/JSON/Parquet
transformation belongs to the tabular/data plane, not this extension.

## Official Google references

- [OAuth 2.0 web-server flow](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Configure OAuth consent](https://developers.google.com/workspace/guides/configure-oauth-consent)
- [Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Google Sheets API scopes](https://developers.google.com/workspace/sheets/api/scopes)
- [Google Picker overview](https://developers.google.com/workspace/drive/picker/guides/overview)
- [Google Picker setup](https://developers.google.com/workspace/drive/picker/guides/web-picker)
- [Control Workspace app access](https://support.google.com/a/answer/7281227)
- [OAuth app verification](https://support.google.com/cloud/answer/13463073)
- [Restricted Gmail scopes](https://support.google.com/cloud/answer/13464325)
- [Security assessment requirements](https://support.google.com/cloud/answer/13465431)
