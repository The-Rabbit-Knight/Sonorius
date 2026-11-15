# Bulk Import Directory

This folder allows you to bulk import questions, answers, and optional diagrams into Sonorius in one click.

## Quick Instructions:

1. **`questions_template.csv`**:
   - Open in Excel, Google Sheets, or any text editor.
   - Fill in your questions and scripted answers.
   - Must have 3 columns:
     - `Question` (Required)
     - `Answer` (Required)
     - `Image` (Optional: filename of an image or diagram, e.g. `Sonorius_image.jpeg`, or leave empty if text-only).

2. **`images/` Subfolder**:
   - Place all image files referenced in the `Image` column (`.png`, `.jpg`, `.jpeg`, `.webp`) inside `bulk_import/images/`.

3. **In the Web App**:
   - Navigate to **"Add New Q&A"** -> expand **"Bulk Import Questions via CSV"**.
   - Upload your CSV and click **"Import Questions to Vault"**.
