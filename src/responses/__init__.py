"""
[local patch] OpenAI Responses API (/v1/responses) for agy2api.

Design (option C, see /workspace/reports/responses_api_feasibility_2026-10-27.md):
  request : Responses body -> chat-style dict (built by hand, never via the pydantic
            OpenAIChatCompletionRequest) -> upstream convert_openai_to_gemini_request
            -> upstream normalize_antigravity_request -> small Responses-only post-steps
  response: Gemini SSE / JSON -> Responses events directly (per-stream state machine)

All upstream functions used are imported only through ``upstream_api`` so a rebase
that changes their signatures fails loudly in tests/responses/test_contract.py.
"""
