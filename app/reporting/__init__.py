"""
Dynamic report templates.

    docx_parser  - structure of any DOCX: blocks, sheets, value slots with label context
    automapper   - slot -> registry field (placeholders, previous version, fuzzy labels, Gemini)
    sections     - text-anchored sections tied to test codes, sheet visibility and numbering
    compiler     - mapping -> docxtpl-tagged DOCX that keeps all original formatting
    generator    - snapshot + compiled template -> filled DOCX
    registry     - value namespace (fields, computed, system values, repeating lists)
    templates    - versioned template records (draft -> pending_hod -> active -> retired)
"""
