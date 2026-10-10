"""
Automatic document reading (Google Gemini via the google-genai SDK).

    client.py           connection (AI Studio / Vertex), model discovery, structured calls, task pool
    intake.py           read_document(path, doc_kind): PDF, images, Excel (own template or any layout), CSV
    template_mapper.py  proposals for report-template slots the deterministic auto-mapper could not map
    prompts/            versioned prompts per document kind

With AI_ENABLED=false or no key nothing leaves the lab network; our own Excel templates are still read
deterministically and every other screen falls back to manual entry.
"""
