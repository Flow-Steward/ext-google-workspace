(function installGoogleWorkspacePickerBridge(globalObject) {
  "use strict";

  const PICKER_SCOPE = "https://www.googleapis.com/auth/drive.file";
  const LOAD_TIMEOUT_MS = 10000;

  function boundedText(value, maximum) {
    return typeof value === "string" && value.trim() === value && value.length <= maximum
      ? value
      : "";
  }

  function validBootstrap(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return null;
    }
    const result = {
      clientId: boundedText(value.client_id, 512),
      developerKey: boundedText(value.developer_key, 512),
      appId: boundedText(value.app_id, 64),
      origin: boundedText(value.origin, 2048),
      emailHint: boundedText(value.email_hint, 320),
      scope: boundedText(value.scope, 256),
    };
    if (
      !result.clientId
      || !result.developerKey
      || !/^\d+$/u.test(result.appId)
      || result.scope !== PICKER_SCOPE
      || result.origin !== globalObject.location.origin
    ) {
      return null;
    }
    return result;
  }

  function pickerView(picker, resourceKind) {
    if (resourceKind === "spreadsheet") {
      return new picker.DocsView(picker.ViewId.SPREADSHEETS);
    }
    const view = new picker.DocsView(picker.ViewId.DOCS);
    const folders = resourceKind === "folder";
    view.setIncludeFolders(folders);
    view.setSelectFolderEnabled(folders);
    return view;
  }

  function selectionRows(picker, data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      return [];
    }
    const documents = data[picker.Response.DOCUMENTS];
    if (!Array.isArray(documents) || documents.length !== 1) {
      return [];
    }
    const document = documents[0];
    if (!document || typeof document !== "object" || Array.isArray(document)) {
      return [];
    }
    const id = boundedText(document[picker.Document.ID], 1024);
    const name = boundedText(document[picker.Document.NAME], 512);
    const mimeType = boundedText(document[picker.Document.MIME_TYPE], 255);
    const parents = Array.isArray(document[picker.Document.PARENTS])
      ? document[picker.Document.PARENTS]
        .slice(0, 16)
        .map((value) => boundedText(value, 1024))
        .filter(Boolean)
      : [];
    return id && name && mimeType
      ? [{ id, name, mime_type: mimeType, parent_ids: parents }]
      : [];
  }

  function loadPickerApi() {
    return new Promise((resolve, reject) => {
      if (!globalObject.gapi || typeof globalObject.gapi.load !== "function") {
        reject(new Error("google_picker_unavailable"));
        return;
      }
      const timeout = globalObject.setTimeout(
        () => reject(new Error("google_picker_unavailable")),
        LOAD_TIMEOUT_MS,
      );
      globalObject.gapi.load("picker", () => {
        globalObject.clearTimeout(timeout);
        if (!globalObject.google?.picker) {
          reject(new Error("google_picker_unavailable"));
          return;
        }
        resolve(globalObject.google.picker);
      });
    });
  }

  function requestPickerToken(bootstrap) {
    return new Promise((resolve, reject) => {
      const oauth = globalObject.google?.accounts?.oauth2;
      if (!oauth || typeof oauth.initTokenClient !== "function") {
        reject(new Error("google_identity_unavailable"));
        return;
      }
      const client = oauth.initTokenClient({
        client_id: bootstrap.clientId,
        scope: PICKER_SCOPE,
        include_granted_scopes: false,
        hint: bootstrap.emailHint,
        callback(response) {
          const token = boundedText(response?.access_token, 8192);
          const scopes = new Set(
            boundedText(response?.scope, 1024).split(/\s+/u).filter(Boolean),
          );
          if (!token || scopes.size !== 1 || !scopes.has(PICKER_SCOPE)) {
            reject(new Error("google_picker_scope_mismatch"));
            return;
          }
          resolve(token);
        },
        error_callback() {
          reject(new Error("google_picker_authorization_failed"));
        },
      });
      client.requestAccessToken({ prompt: "" });
    });
  }

  async function open({ bootstrap: rawBootstrap, options }) {
    const bootstrap = validBootstrap(rawBootstrap);
    const resourceKind = options?.resource_kind;
    if (!bootstrap || !["file", "folder", "spreadsheet"].includes(resourceKind)) {
      throw new Error("invalid_google_picker_request");
    }
    const [picker, token] = await Promise.all([
      loadPickerApi(),
      requestPickerToken(bootstrap),
    ]);
    return new Promise((resolve, reject) => {
      const dialog = new picker.PickerBuilder()
        .addView(pickerView(picker, resourceKind))
        .setAppId(bootstrap.appId)
        .setDeveloperKey(bootstrap.developerKey)
        .setOAuthToken(token)
        .setOrigin(bootstrap.origin)
        .setCallback((data) => {
          const action = data?.[picker.Response.ACTION];
          if (action === picker.Action.CANCEL) {
            resolve([]);
            return;
          }
          if (action !== picker.Action.PICKED) {
            return;
          }
          const rows = selectionRows(picker, data);
          if (!rows.length) {
            reject(new Error("invalid_google_picker_selection"));
            return;
          }
          resolve(rows);
        })
        .build();
      dialog.setVisible(true);
    });
  }

  globalObject.FlowStewardGoogleWorkspacePicker = Object.freeze({ open });
}(globalThis));
