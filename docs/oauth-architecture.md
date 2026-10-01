# Google Workspace extension-owned OAuth architecture

This extension declares and owns its OAuth provider contract, Setup fields, consent
intents, provider endpoints, scopes, help copy, and post-connect identity action. The
host validates that closed contract, stores each project's configuration and connection
secrets, performs the generic PKCE/state/token protocol, and invokes extension actions
through the ordinary runtime boundary. Host code does not contain Google endpoints,
scopes, credential roles, intent names, or provider-specific lifecycle orchestration.

Installation Client credentials and Picker configuration are entered on this
extension's Setup page. Account grants are separate project-scoped connections. A
successful callback persists the grant before `verify_google_identity` runs; the action
returns the stable subject and safe profile metadata through the declared result
contract. Retry is available only for a retained post-connect failure. Disconnect and
uninstall use the generic host lifecycle and secret cleanup paths.

The runtime receives only host-resolved credentials for the selected connection. It
never receives an authorization code or OAuth Client Secret. Browser Picker tokens stay
in browser memory and are neither persisted nor forwarded to the extension runtime.

This document deliberately lives with the extension so the ownership boundary remains
accurate if the bundle is extracted from the host repository.
