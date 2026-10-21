# Vendor endpoint snapshot

Captured on 2026-10-21 from OpenRouter's public endpoints API:

- https://openrouter.ai/api/v1/models/minimax/minimax-m2.1/endpoints
- https://openrouter.ai/api/v1/models/minimax/minimax-m2.5/endpoints
- https://openrouter.ai/api/v1/models/minimax/minimax-m2.7/endpoints

`minimax-endpoints.json` groups the raw responses by model id, reduced to
the fields the vendor pass reads: `tag`, `provider_name`, `quantization`,
`context_length`, `max_completion_tokens`, `max_prompt_tokens`, and the
whole `pricing` object. Every endpoint is retained, including third-party
hosts, in its captured order. Replay this fixture without a live fetch.
