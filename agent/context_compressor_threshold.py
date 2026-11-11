"""Lazy compression trigger evaluation, including the selected route's request budget."""


def threshold_tokens(compressor):
    if compressor._threshold_tokens is None:
        # Resolve the window first: it may floor threshold_percent as a side effect.
        context = compressor.context_length
        compressor._threshold_tokens = compressor._compute_threshold_tokens(
            context, compressor.threshold_percent, compressor.max_tokens)
        compressor._apply_threshold_tokens_cap()
    threshold = compressor._threshold_tokens
    for name in ("_aux_context_ceiling", "_route_context_ceiling"):
        cap = getattr(compressor, name, None)
        if isinstance(cap, int):
            threshold = min(threshold, cap)
    return threshold
