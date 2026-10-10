Rules that apply to every document:
- The source is a document of the CPRI Short Circuit Laboratory, Bengaluru (Central Power Research Institute).
- Return JSON that follows the given schema exactly: one entry per field you can read, using the field keys listed below.
- If a field is blank, missing or unreadable, return value null with confidence 0. Never guess.
- Transcribe handwriting exactly as written. Do not "correct" serial numbers, codes or readings.
- Ignore anything that is struck through or crossed out. Use the corrected value written next to it, if any.
- A ticked box or a circled option means that option is selected (true); unticked means false.
- confidence is 0 to 1: how sure you are that the value is exactly right.
- source: where the value is - "pdf_page N" for page images, or the cell address "Sheet!B4" for spreadsheets.
- evidence_text: the exact words or digits you read (at most 80 characters).
