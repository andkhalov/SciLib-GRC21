"""DeepSeek-Prover-V2 model loading and generation.

Uses chat template as required by the model (not raw tokenization).
Lazy-loaded: model is initialized on first generate() call.
"""

import re
import logging

from .config import MODEL_ID, LEAN_WRAPPER, MODEL_TEMPERATURE

log = logging.getLogger("model")

_tokenizer = None
_model = None


def init():
    """Load model and tokenizer onto GPU. Called once."""
    global _tokenizer, _model
    if _model is not None:
        return

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.manual_seed(30)
    log.info("Loading model %s ...", MODEL_ID)

    _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    _model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID,
        device_map="auto",
        torch_dtype=torch.bfloat16,
        trust_remote_code=True,
    ).to("cuda")

    log.info("Model loaded on %s", _model.device)


def generate(
    prompt: str,
    max_new_tokens: int = 8192,
    temperature: float = MODEL_TEMPERATURE,
    top_p: float = 0.95,
    top_k: int = 40,
    repetition_penalty: float = 1.05,
    do_sample: bool = True,
) -> str:
    """Generate text from prompt using chat template.

    The prompt is wrapped as a user message in DeepSeek chat format:
    <BOS><｜User｜>{prompt}<｜Assistant｜>

    Returns raw decoded output (before cleaning).
    """
    if _model is None:
        init()

    chat = [{"role": "user", "content": prompt}]
    encoded = _tokenizer.apply_chat_template(
        chat, tokenize=True, add_generation_prompt=True, return_tensors="pt",
    )
    # apply_chat_template may return a Tensor or a BatchEncoding depending on version
    input_ids = (encoded.input_ids if hasattr(encoded, "input_ids") else encoded).to(_model.device)

    out = _model.generate(
        input_ids,
        max_new_tokens=max_new_tokens,
        do_sample=do_sample,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        repetition_penalty=repetition_penalty,
    )

    in_len = input_ids.shape[1]
    return _tokenizer.decode(out[0, in_len:], skip_special_tokens=False)


def extract_lean_code(raw: str) -> str:
    """Extract Lean 4 code from model output.

    The model may output:
    1. Non-CoT: direct Lean code (possibly in a ```lean4 fence)
    2. CoT: NL proof plan followed by Lean code in a ```lean4 fence

    In both cases, we extract the Lean code block.
    If no code fence found, treat entire output as Lean code.
    """
    # Strip EOS token
    text = raw.split("<｜end▁of▁sentence｜>")[0].strip()

    # Try to extract from ```lean4 ... ``` or ```lean ... ``` fence
    fence_match = re.search(
        r'```(?:lean4?)\s*\n([\s\S]*?)```', text)
    if fence_match:
        return fence_match.group(1).strip()

    # Try plain ``` ... ``` fence
    fence_match = re.search(r'```\s*\n([\s\S]*?)```', text)
    if fence_match:
        return fence_match.group(1).strip()

    # No fence — clean up and return as-is (Non-CoT direct output)
    text = re.sub(r"```+", "", text)
    return text.strip()


def ensure_lean_imports(code: str) -> str:
    """Ensure the code has Mathlib imports for Lean checking.

    If the model output already contains 'import Mathlib', use as-is.
    Otherwise prepend the standard wrapper.
    """
    if "import Mathlib" in code:
        return code
    return LEAN_WRAPPER + code
