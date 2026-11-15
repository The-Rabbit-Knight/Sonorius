# Bulk CSV Import & Content Ingestion Guide

Sonorius includes a **Bulk Import** tool to import questions, answers, and images or diagrams from spreadsheets directly into your local vault.

---

## 1. CSV Format & Column Requirements

Your `.csv` file must contain at least two primary columns, with an optional third column for diagrams:

| Column Name | Required? | Accepted Header Aliases | Description |
| :--- | :--- | :--- | :--- |
| `Question` | **Yes** | `question`, `q`, `question_text` | The full text of the question or prompt. |
| `Answer` | **Yes** | `answer`, `a`, `answer_text` | The complete, possible verbal answer. |
| `Image` | Optional | `image`, `picture`, `image_name`, `image_path`, `img` | Filename of the associated image or diagram (e.g. `Sonorius_image.jpeg`). Leave empty if none. |

### Starter CSV Example

```csv
Question,Answer,Image
"What is Sonorius and how does it help preparation?","Sonorius is an offline-first, voice-assisted study and rehearsal platform. You can practice actively with a custom think-time pause to articulate your answers out loud before the saved answer plays, or listen continuously without pauses while reviewing architecture diagrams...","Sonorius_image.jpeg"
"How does the two-voice dialogue work?","In the sidebar under 'Preferences', Sonorius allows you to configure a two-voice dialogue. You can assign one voice to the Interviewer (who reads the questions) and a different voice to the Candidate (who reads your curated answers)...",""
```

A clean template file is provided in `bulk_import/questions_template.csv`.

---

## 2. Managing Diagrams and Images

You can associate images with your questions using either of two workflows:

### Method A: Local Folder Placement (Fastest for Large Batches)
1. Place all your diagram image files (`.png`, `.jpg`, `.jpeg`, `.webp`) inside the `bulk_import/images/` directory.
2. In your CSV, set the `Image` column to the exact filename (e.g. `Sonorius_image.jpeg`).
3. Upload only the CSV via the web dashboard. Sonorius will automatically match each image filename from `bulk_import/images/`, assign it a collision-free UUID, and copy it to the permanent image vault.

### Method B: Web Multi-File Upload
1. In the **Bulk Import** expander, upload your CSV in uploader **1**.
2. Drag and drop all your image files simultaneously into uploader **2** (*Upload Referenced Diagrams*).
3. Sonorius will match the filenames referenced in the CSV rows with the uploaded files in memory and commit them to disk.

---

## 3. Pre-Flight Validation & Error Safety

When you upload a CSV, Sonorius performs automatic pre-flight checks before saving:
- **Encoding Fallback:** Automatically tries `UTF-8` first, then safely falls back to `Latin-1` to prevent crash on non-ASCII characters or smart quotes.
- **Row Sanitization:** Empty rows or rows missing either Question or Answer are safely skipped.
- **Field Limits & Formula Safety:** Questions are limited to **2,000 characters**, Answers to **10,000 characters**, and dangerous formula/DDE patterns are blocked. Validation is all-or-nothing across the batch: if any row fails these checks, the entire import is rejected so partial data is not inserted.
- **Preview Table:** Renders a preview of the first 5 parsed rows and diagram assignments for visual inspection.
- **Missing Image Warnings:** If a CSV references an image that cannot be found, Sonorius imports the question anyway without failing, and logs a clear warning list of missing filenames.

---

## 4. Organizing Questions into Playlists

After importing questions to your vault:
1. Navigate to **Manage Playlists** in the sidebar.
2. Expand **Create New Playlist**.
3. Enter a playlist name (e.g., *Exam Review*, *Presentation Prep*, or *Study Deck*).
4. Use the scrollable question checklist picker to tick the questions you want in this playlist (with real-time keyword filtering and **Select All** / **Deselect All** controls).
5. In the interactive play-order table below, change the **Order** numbers if you want to rearrange their playback sequence.
6. Click **Save Playlist**. The playlist is immediately ready for voice practice under **Play Session**.
