# Workflow actions

Create and authorize the matching Gmail, Google Drive or Google Sheets project connection, then select it for this step. Drive and Sheets files must be available to the app through Google Picker or created by the app.

Select the connection in the workflow step. Map returned message IDs, file handles or dataset handles into later steps rather than pasting credentials or local paths.

## Test Gmail connection (`test_connection`)

Check that the selected Gmail account is authorized and reachable. Create and authorize a Gmail project connection, then select it for this step. Example: Verify authorization before running a Gmail intake workflow.

Result fields: `email_address`, `messages_total`, `threads_total`, `history_id`.

## List Gmail labels (`list_labels`)

List the Gmail account's labels so later steps can select the correct mailbox category. Create and authorize a Gmail project connection, then select it for this step. Example: Find INBOX and a processed label before searching or organizing messages.

Result fields: `labels`.

## Search Gmail messages (`search_messages`)

Search one Gmail label with typed filters and return message IDs and a next-page cursor. Create and authorize a Gmail project connection, then select it for this step. Example: Find unread invoices under INBOX with filters {"subject":"invoice","read_state":"unread"}.

Inputs:

- `label_id`: Select the Gmail label to search, such as INBOX.
- `filters`: JSON object using from, to, cc, subject, text, rfc_message_id, since/before (YYYY-MM-DD), read_state (any/read/unread), star_state (any/starred/unstarred), attachment_state (any/with_attachments/without_attachments).
- `limit`: Maximum messages per page, from 1 to 200; defaults to 50.
- `cursor`: Leave empty for the first page. For later pages, map next_cursor returned by this search and keep the same connection, filters and page size.

Result fields: `messages`, `next_cursor`, `truncated`.

## Read Gmail message (`get_message`)

Read one Gmail message by its provider message ID, optionally returning body, headers and attachment metadata. Create and authorize a Gmail project connection, then select it for this step. Example: Read a message_id returned by Search Gmail messages and inspect its attachments.

Inputs:

- `message_id`: Gmail provider message ID returned by Search Gmail messages; this is not an email Message-ID header.
- `include_body`: Include the message body in the result for downstream text processing.
- `include_headers`: Include the message headers in the result.
- `include_attachment_metadata`: Include attachment identifiers, filenames and content types so a later download step can select a file.
- `include_raw_html`: Include the original HTML body when HTML content is needed by a later step.

Result fields: `message`.

## Download Gmail attachment (`get_attachment`)

Download one Gmail attachment into a workflow file artifact for later steps. Create and authorize a Gmail project connection, then select it for this step. Example: Download a selected invoice attachment and pass the resulting artifact to document extraction.

Inputs:

- `message_id`: Gmail provider message ID returned by Search Gmail messages; this is not an email Message-ID header.
- `attachment_id`: Attachment ID from the selected message's attachment metadata. Use it with that same message_id.

Result fields: `attachment_artifact_handle`, `attachment_metadata`.

## Set Gmail read and starred flags (`set_message_flags`)

Change read and starred state for selected Gmail message IDs; omitted flags keep their current state. Create and authorize a Gmail project connection, then select it for this step. Example: Set seen=true after successful processing and leave flagged empty to preserve stars.

Inputs:

- `message_ids`: JSON array of Gmail provider message IDs returned by search.
- `seen`: Set true to mark messages read or false to mark them unread. Leave empty to preserve read state; provide at least one of seen or flagged.
- `flagged`: Set true to star messages or false to remove the star. Leave empty to preserve starred state; provide at least one of seen or flagged.

Result fields: `results`, `external_effect_status`, `definitely_no_external_effect`.

## Move Gmail messages between labels (`move_messages`)

Organize selected Gmail messages by removing a source label and adding a target label. Create and authorize a Gmail project connection, then select it for this step. Example: Remove an Inbox label and add a Processed label after downstream work succeeds.

Inputs:

- `message_ids`: JSON array of Gmail provider message IDs returned by search.
- `source_label_id`: Select the label to remove from the chosen messages.
- `target_label_id`: Select the label to add to the chosen messages.

Result fields: `results`, `external_effect_status`, `definitely_no_external_effect`.

## Move Gmail messages to Trash (`delete_messages`)

Move selected Gmail messages to Trash. Create and authorize a Gmail project connection, then select it for this step. Example: Move duplicate invoice messages to Trash after reviewing the selected message IDs.

Inputs:

- `message_ids`: JSON array of Gmail provider message IDs returned by search.

Result fields: `results`, `external_effect_status`, `definitely_no_external_effect`.

## Search Google Drive files (`search_drive_files`)

Search the app-visible Drive corpus with bounded typed filters and a filter-bound cursor. Create and authorize a Google Drive project connection, then select it for this step. Select existing files and folders with Google Picker so the app can access them. Example: Find PDF invoices in an app-authorized folder with mime_types=["application/pdf"].

Inputs:

- `parent_folder_id`: Select with Google Picker or map an upstream folder ID.
- `name`: Filename to match using the chosen exact or contains mode.
- `name_match`: contains matches part of the filename; exact requires the full filename.
- `mime_types`: JSON array of MIME type strings used to restrict the search.
- `modified_after`: Return files modified after this RFC 3339 date and time, including a timezone.
- `modified_before`: Return files modified before this RFC 3339 date and time, including a timezone.
- `trashed`: False searches files outside Trash. True searches only trashed files.
- `sort`: provider_order keeps Drive order; name_asc sorts by filename; modified_time_desc returns newest modified first.
- `limit`: Maximum files per page, from 1 to 100; defaults to 50.
- `cursor`: Map only a cursor returned by this operation with the same filters.

Result fields: `files`, `next_cursor`.

## Download Google Drive file (`download_drive_file`)

Stream one selected Drive file or explicit supported native export into an artifact grant. Create and authorize a Google Drive project connection, then select it for this step. Select existing files and folders with Google Picker so the app can access them. Example: Download a selected PDF, or export an app-authorized native document as PDF for extraction.

Inputs:

- `file_id`: Select with Google Picker or map an upstream file ID.
- `export_mime_type`: For a supported Google native file, explicitly select PDF or Excel export as appropriate. Leave empty for ordinary binary files.

Result fields: `artifact_handle`, `filename`, `mime_type`, `size`, `sha256`, `file_id`, `modified_time`.

## Upload file to Google Drive (`upload_drive_file`)

Stream an owned artifact into one Picker-authorized Drive folder. Create and authorize a Google Drive project connection, then select it for this step. Select existing files and folders with Google Picker so the app can access them. Example: Save a generated report artifact as daily-report.pdf in a Picker-authorized folder.

Inputs:

- `artifact_handle`: Map the file artifact handle returned by an earlier download or file-generation step. The file must be available to this workflow run.
- `destination_folder_id`: Select a folder with Google Picker or map an upstream folder ID.
- `filename`: Filename for the uploaded artifact, including its extension.

Result fields: `file_id`, `name`, `mime_type`, `size`, `web_view_link`.

## Read Google Sheet (`read_google_sheet`)

Stream one selected sheet range into a bounded NDJSON dataset grant. Create and authorize a Google Sheets project connection, then select it for this step. Select existing files and folders with Google Picker so the app can access them. Example: Read A1:D100 on the Orders sheet with the first row used as field names.

Inputs:

- `spreadsheet_id`: Select with Google Picker or map an upstream spreadsheet ID.
- `sheet_id`: Enter exactly one of Sheet ID or Sheet title.
- `sheet_title`: Enter exactly one of Sheet ID or Sheet title.
- `range`: A1 range relative to the selected sheet, without a sheet prefix. Leave empty for the supported default read range.
- `header_mode`: first_row uses the first row as dataset field names; none reads rows without treating the first row as a header.

Result fields: `dataset_handle`, `row_count`, `column_count`, `schema`, `preview`, `spreadsheet_id`, `sheet_id`.

## Create or overwrite Google Sheet (`write_google_sheet`)

Create a spreadsheet or overwrite one selected sheet rectangle with RAW values. Create and authorize a Google Sheets project connection, then select it for this step. Select existing files and folders with Google Picker so the app can access them. Example: Create a report spreadsheet from inline_rows=[["Order","Total"],["A-100",42.5]], or overwrite a selected rectangle starting at A1.

Inputs:

- `mode`: create makes a new spreadsheet; overwrite writes to the selected existing sheet starting at start_cell.
- `dataset_handle`: Choose exactly one input dataset or inline rows.
- `inline_rows`: Choose exactly one input dataset or inline rows; maximum 100 rows.
- `start_cell`: Single A1 cell relative to the selected sheet. Defaults to A1.
- `spreadsheet_title`: Name of the spreadsheet to create in create mode.
- `destination_folder_id`: Select an app-authorized destination folder with Google Picker or map an authorized folder ID.
- `spreadsheet_id`: Select an app-authorized spreadsheet with Google Picker or map its ID; use this for overwrite or append operations.
- `sheet_id`: Numeric sheet tab ID, zero or greater. Provide exactly one of sheet_id or sheet_title for an existing sheet.
- `sheet_title`: Exact sheet tab title. Provide exactly one of sheet_title or sheet_id for an existing sheet.

Result fields: `spreadsheet_id`, `sheet_id`, `web_view_link`, `row_count`, `column_count`.

## Append Google Sheet rows (`append_google_sheet_rows`)

Append RAW rows after one selected logical table using a stable chunk marker. Create and authorize a Google Sheets project connection, then select it for this step. Select existing files and folders with Google Picker so the app can access them. Example: Append processed order rows after the logical table in A:D without replacing its existing rows.

Inputs:

- `spreadsheet_id`: Select an app-authorized spreadsheet with Google Picker or map its ID; use this for overwrite or append operations.
- `sheet_id`: Enter exactly one of Sheet ID or Sheet title.
- `sheet_title`: Enter exactly one of Sheet ID or Sheet title.
- `table_range`: Relative A1 range identifying the logical table after which rows are appended.
- `dataset_handle`: Choose exactly one input dataset or inline rows.
- `inline_rows`: Choose exactly one input dataset or inline rows; maximum 100 rows.

Result fields: `spreadsheet_id`, `sheet_id`, `appended_range`, `row_count`, `column_count`.
