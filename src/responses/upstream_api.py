"""
[local patch] Single import surface for the upstream functions the Responses
implementation depends on (including a few private helpers). Keep this list small;
tests/responses/test_contract.py checks their signatures after every rebase.
"""
from src.converter.openai2gemini import (  # noqa: F401
    convert_openai_to_gemini_request,
    _convert_usage_metadata,
    _reverse_transform_args,
    _normalize_function_name,
)
from src.converter.antigravity_fix import (  # noqa: F401
    normalize_antigravity_request,
    resolve_profile,
    _apply_thinking,
    MAX_OUTPUT_TOKENS,
)
from src.converter.thoughtSignature_fix import (  # noqa: F401
    SKIP_THOUGHT_SIGNATURE_VALIDATOR,
    is_skip_thought_signature_placeholder,
    is_internal_placeholder_text,
)
from src.converter.anti_truncation import (  # noqa: F401
    AntiTruncationStreamProcessor,
    apply_anti_truncation,
    SYNTHETIC_TOOL_NAME,
)
from src.utils import (  # noqa: F401
    is_anti_truncation_model,
    get_base_model_from_feature_model,
    authenticate_bearer,
)
from src.api.antigravity import stream_request  # noqa: F401
from src.api.antigravity_routing import classify  # noqa: F401
