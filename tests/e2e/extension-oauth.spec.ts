import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

type AcceptanceState = {
  alpha_project_id: string;
  consumer_extensions: Record<string, string>;
};

const statePath = process.env.FS_OAUTH_ACCEPTANCE_STATE;
if (!statePath) {
  throw new Error("FS_OAUTH_ACCEPTANCE_STATE is required");
}
const acceptance = JSON.parse(readFileSync(statePath, "utf8")) as AcceptanceState;
const extensionId = "flowsteward.google-workspace";
const operator = {
  email: process.env.E2E_OPERATOR_USERNAME || "",
  password: process.env.E2E_OPERATOR_PASSWORD || "",
};

test("shipped extension owns its OAuth copy, fields, intents, endpoints, and Picker UI", async ({ page }) => {
  const login = await page.request.post("/api/auth/login", {
    data: { ...operator, remember_me: false },
  });
  expect(login.status()).toBe(200);

  await page.goto(
    `/app/extensions/${extensionId}`
    + `?project_id=${acceptance.alpha_project_id}`,
  );
  await expect(page.getByRole("tab", { name: "Setup guide", selected: true })).toBeVisible();
  const oauthSetup = page.getByRole("region", { name: "Google Workspace OAuth setup" });
  await expect(oauthSetup.getByRole("heading", { name: "Google Workspace" })).toBeVisible();
  await expect(page.getByText("Google Workspace setup guide")).toBeVisible();
  const clientId = page.getByLabel("Google OAuth Client ID");
  const clientSecret = page.getByLabel("Google OAuth Client Secret");
  const pickerKey = page.getByLabel("Picker API key");
  const pickerAppId = page.getByLabel("Picker App ID");
  const pickerOrigin = page.getByLabel("Picker browser origin");
  await expect(clientId).toHaveAttribute("type", "text");
  await expect(clientSecret).toHaveAttribute("type", "password");
  await expect(clientId).toHaveAttribute("aria-required", "true");
  await expect(clientSecret).toHaveAttribute("aria-required", "true");
  await expect(page.getByText(/^Google OAuth Client ID\s*\*$/u)).toBeVisible();
  await expect(page.getByText(/^Google OAuth Client Secret\s*\*$/u)).toBeVisible();
  for (const optional of [pickerKey, pickerAppId, pickerOrigin]) {
    await expect(optional).toBeVisible();
    await expect(optional).not.toHaveAttribute("aria-required", "true");
  }
  for (const optionalLabel of [/^Picker API key\s*\*$/u, /^Picker App ID\s*\*$/u, /^Picker browser origin\s*\*$/u]) {
    await expect(page.getByText(optionalLabel)).toHaveCount(0);
  }
  const redirectUri = page.getByLabel("OAuth Redirect URI");
  await expect(redirectUri).toHaveAttribute("readonly", "");
  const registeredRedirect = new URL(await redirectUri.inputValue());
  expect(registeredRedirect.origin).toBe(
    process.env.FS_OAUTH_EXPECTED_PUBLIC_BASE_URL || "https://localhost:8099",
  );
  expect(registeredRedirect.pathname).toMatch(
    /^\/webhook\/extensions\/flowsteward\.google-workspace\/oauth_authorization\/[^/]+$/u,
  );
  expect(registeredRedirect.search).toBe("");
  expect(registeredRedirect.hash).toBe("");
  for (const pageName of ["Gmail", "Drive", "Sheets"]) {
    const serviceTab = page.getByRole("tab", { name: pageName });
    await expect(serviceTab).toBeVisible();
    await expect(serviceTab).toBeDisabled();
  }

  await page.getByLabel("Configuration name").fill("Disposable Google application");
  const save = page.getByRole("button", { name: "Save configuration" });
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.getByText("Google OAuth Client ID is required.")).toBeVisible();
  await expect(page.getByText("Google OAuth Client Secret is required.")).toBeVisible();
  for (const required of [clientId, clientSecret]) {
    await expect(required).toHaveAttribute("aria-invalid", "true");
    await expect(required).toHaveAttribute("aria-describedby", /error/u);
  }
  await expect(clientId).toBeFocused();

  await clientId.fill("disposable-client.apps.example.test");
  await clientSecret.fill(["oauth", "acceptance", "credential"].join("-"));
  await pickerKey.fill(["picker", "acceptance", "key"].join("-"));
  await pickerAppId.fill("1234567890");
  await pickerOrigin.fill("https://browser.example.test");
  await save.click();
  await expect(page.getByRole("status").filter({ hasText: /^Configuration saved\./u })).toBeVisible();
  const configurationSelector = page.getByRole("combobox", { name: "OAuth configuration" });
  await expect(configurationSelector).toHaveText("Disposable Google application");
  await configurationSelector.click();
  const savedOption = page.getByRole("listbox", { name: "OAuth configuration options" })
    .getByRole("option", { name: "Disposable Google application", exact: true });
  await expect(savedOption).toBeVisible();
  await expect(savedOption).toHaveAttribute("aria-selected", "true");
  await configurationSelector.press("Escape");
  await expect(configurationSelector).toHaveAttribute("aria-expanded", "false");
  for (const pageName of ["Gmail", "Drive", "Sheets"]) {
    await expect(page.getByRole("tab", { name: pageName })).toBeEnabled();
  }

  expect(acceptance.consumer_extensions[extensionId]).toBeTruthy();
  const compiledManifest = await page.request.get(
    `/api/extensions/${extensionId}/compiled-manifest`,
  );
  expect(compiledManifest.status()).toBe(200);
  const serialized = JSON.stringify(await compiledManifest.json());
  expect(serialized).toContain("https://accounts.google.com/o/oauth2/v2/auth");
  expect(serialized).toContain("https://oauth2.googleapis.com/token");
  expect(serialized).toContain("https://oauth2.googleapis.com/revoke");
  expect(serialized).toContain("gmail_connect");
  expect(serialized).toContain('"test_action":"test_connection"');
  expect(serialized).toContain("drive_connect");
  expect(serialized).toContain("sheets_connect");

  await page.getByRole("tab", { name: "Gmail" }).click();
  await expect(page.getByRole("tab", { name: "Gmail", selected: true })).toBeVisible();
  await expect(page.getByText(
    "Connect Google accounts and manage Gmail access for this project.",
  )).toBeVisible();
  await expect(page.getByRole("form", { name: "Connect an account" })).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "Configuration for new connection" }),
  ).toHaveCount(0);
  await expect(page.getByLabel("Connection type")).toHaveCount(0);

  await page.getByRole("tab", { name: "Drive" }).click();
  await expect(page.getByRole("tab", { name: "Drive", selected: true })).toBeVisible();
  await expect(page.getByText(
    "Enable bounded Drive access and select files or folders with Google Picker.",
  )).toBeVisible();
});
