import { render, screen } from "@testing-library/react";

import { createWorkflowConnectorRenderers } from "@/pages/workflows/detail/detail-connector-renderers";
import fixture from "./google-picker-contract-fixture.json";

function declarationFor(row: (typeof fixture.cases)[number]) {
  return {
    ...fixture.common,
    button_label: row.button_label,
    error_message: row.error_message,
    options: { resource_kind: row.resource_kind },
    selection: {
      maximum: 1,
      fields: [{ source: "id", target: row.field_name, required: true, max_length: 1024 }],
    },
  };
}

describe("bundled Google Workspace picker production path", () => {
  it.each(fixture.cases)(
    "renders $operation_id.$field_name through the generic workflow bridge",
    (row) => {
      const utils = createWorkflowConnectorRenderers({
        connectorOutputRows: [],
        connectorParameterRows: [{
          name: row.field_name,
          label: row.field_name,
          required: true,
          widget_type: "resource_picker",
          external_resource_picker: declarationFor(row),
        }],
        renderWorkflowVariableInput: vi.fn(),
        replaceStepKindConfigField: vi.fn(),
        selectedStep: { raw: {} },
        selectedConnectorId: "extension.flowsteward.google-workspace",
        selectedExtensionInstallId: "install-google-workspace",
        selectedExtensionReleaseId: "release-google-workspace",
        selectedExtensionDescriptorSha256: "a".repeat(64),
        selectedProjectId: "project-google-workspace",
        stepKindConfig: {
          connection_binding: { connection_id: "google-workspace-primary" },
          input_mapping: { [row.field_name]: `literal:${row.field_name}-current` },
        },
      });

      render(utils.renderConnectorInputMappingEditor());

      expect(screen.getByRole("button", { name: row.button_label })).toBeEnabled();
      expect(screen.queryByText("This resource picker is unavailable.")).not.toBeInTheDocument();
    },
  );
});
