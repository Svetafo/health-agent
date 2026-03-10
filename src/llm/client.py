"""Single LLM call point — via LiteLLM (model-agnostic)."""

import base64
import json
import logging
import os
import re
import time
from datetime import date
from typing import Any, Callable

import litellm
import openai
from groq import AsyncGroq

from src.config import settings

log = logging.getLogger(__name__)

# Pass keys into os.environ so LiteLLM can pick them up
# (pydantic-settings reads .env into the Settings object, but not into os.environ)
if settings.openai_api_key:
    os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key)
if settings.anthropic_api_key:
    os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)
if settings.gemini_api_key:
    os.environ.setdefault("GEMINI_API_KEY", settings.gemini_api_key)

# Disable LiteLLM's built-in retries — otherwise every rate limit consumes 4x the quota tokens
litellm.num_retries = 0

# Display names for models shown to the user
_MODEL_LABELS: dict[str, str] = {
    "anthropic/claude-haiku-4-5-20251001": "Haiku",
    "anthropic/claude-haiku-4-5": "Haiku",
    "anthropic/claude-sonnet-4-5-20251022": "Sonnet",
    "anthropic/claude-sonnet-4-5": "Sonnet",
    "anthropic/claude-opus-4-5": "Opus",
    "openai/gpt-4o-mini": "Mini",
    "openai/gpt-4o": "GPT-4o",
}


def model_label(model: str) -> str:
    """Returns the short model name for display to the user."""
    return _MODEL_LABELS.get(model, model.split("/")[-1])


# cost per 1M tokens (input, output) in USD
_COSTS = {
    "openai":    (0.15, 0.60),
    "anthropic": (3.0,  15.0),
    "gemini":    (0.10, 0.40),
}

# history — list of {"role": "user"|"assistant", "content": "..."}
History = list[dict[str, str]]


def _parse_llm_json(text: str) -> dict:
    """Parses JSON from an LLM response, stripping markdown wrappers."""
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text.strip())


def _resolve_model(model: str | None, provider: str | None = None) -> str:
    """Returns the full LiteLLM model-string (provider/model)."""
    if model:
        if "/" in model:
            return model  # already in full format
        p = provider or settings.llm_provider
        return f"{p}/{model}"
    return settings.cheap_model


def _provider_from_model(model: str) -> str:
    if "/" in model:
        return model.split("/")[0]
    return settings.llm_provider


def _log_cost(model: str, in_tok: int, out_tok: int, elapsed: float) -> None:
    p = _provider_from_model(model)
    cost_in, cost_out = _COSTS.get(p, (0.15, 0.60))
    cost = (in_tok / 1_000_000) * cost_in + (out_tok / 1_000_000) * cost_out
    log.info(
        "LLM call: model=%s in=%d out=%d cost=$%.4f time=%.2fs",
        model, in_tok, out_tok, cost, elapsed,
    )


async def transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Transcribes audio via Groq Whisper (free, 2h/day)."""
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY not set")
    client = AsyncGroq(api_key=settings.groq_api_key)
    transcription = await client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model="whisper-large-v3",
        language="ru",
    )
    return transcription.text


async def ask_model(
    prompt: str,
    system: str | None = None,
    history: History | None = None,
    provider: str | None = None,
    model: str | None = None,
    max_tokens: int = 1024,
    return_model: bool = False,
) -> str | tuple[str, str]:
    """Sends a request to the LLM via LiteLLM and returns a text response.

    model can be the full LiteLLM format "provider/name" or just "name".
    If model is not specified — settings.cheap_model is used.
    """
    target = _resolve_model(model, provider)
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    start = time.monotonic()
    try:
        response = await litellm.acompletion(
            model=target,
            messages=messages,
            max_tokens=max_tokens,
            timeout=90,
            num_retries=0,
        )
    except litellm.RateLimitError:
        if target != settings.cheap_model:
            log.warning("ask_model: rate limit on %s, falling back to %s", target, settings.cheap_model)
            target = settings.cheap_model
            response = await litellm.acompletion(
                model=target,
                messages=messages,
                max_tokens=max_tokens,
                timeout=90,
                num_retries=0,
            )
        else:
            raise
    elapsed = time.monotonic() - start
    usage = response.usage
    _log_cost(target, usage.prompt_tokens, usage.completion_tokens, elapsed)
    content = response.choices[0].message.content
    if return_model:
        return content, model_label(target)
    return content


async def embed_text(text: str) -> list[float]:
    """Returns a text embedding via OpenAI text-embedding-3-small (1536 dims).

    Cost: $0.02 / 1M tokens.
    """
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    response = await client.embeddings.create(
        model=settings.embedding_model,
        input=text,
    )
    return response.data[0].embedding


async def _call_vision(
    model: str, image_bytes: bytes, media_type: str, prompt: str, max_tokens: int
) -> tuple[str, int, int]:
    """LiteLLM vision — unified format for OpenAI and Anthropic."""
    b64 = base64.b64encode(image_bytes).decode()
    start = time.monotonic()
    response = await litellm.acompletion(
        model=model,
        max_tokens=max_tokens,
        timeout=90,
        num_retries=0,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    elapsed = time.monotonic() - start
    usage = response.usage
    log.info("vision: model=%s in=%d out=%d time=%.2fs", model, usage.prompt_tokens, usage.completion_tokens, elapsed)
    return response.choices[0].message.content, usage.prompt_tokens, usage.completion_tokens


_NUTRITION_PROMPT = """Analyze the food image and return JSON strictly in this format (no markdown):
{"calories": 500, "protein": 30, "fat": 15, "carbs": 45, "date": null,
 "meals": [{"name": "dish name", "calories": 300}, {"name": "other", "calories": 200}]}

If the image is a screenshot of a nutrition app — use the numbers shown.
If it is a photo of food — estimate the calorie content. date — ISO date if visible in the screenshot, otherwise null.
Return ONLY JSON, no explanations."""


async def analyze_nutrition_image(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """Analyzes a food photo or screenshot via vision LLM → dict with macros."""
    models = [settings.vision_food_model, settings.vision_medical_model]
    last_err: Exception | None = None

    for m in models:
        try:
            text, _, _ = await _call_vision(m, image_bytes, media_type, _NUTRITION_PROMPT, 512)
            return _parse_llm_json(text)
        except Exception as e:
            log.warning("analyze_nutrition_image via %s failed: %s", m, e)
            last_err = e

    raise RuntimeError(f"analyze_nutrition_image failed: {last_err}")


async def analyze_nutrition_text(text: str) -> dict:
    """Parses a text food description → dict with macros via ask_model."""
    prompt = (
        f'The user described food: "{text}"\n'
        "Return JSON strictly in this format (no markdown):\n"
        '{"calories": 500, "protein": 30, "fat": 15, "carbs": 45, "date": null,\n'
        ' "meals": [{"name": "name", "calories": 300}]}\n'
        "Estimate macros as accurately as possible. date — ISO date if mentioned, otherwise null.\n"
        "Return ONLY JSON."
    )
    raw = await ask_model(prompt, max_tokens=512)
    return _parse_llm_json(raw)


async def classify_food_input(text: str) -> str:
    """Determines what the user wrote in /food mode: 'log' or 'report'."""
    prompt = (
        f'The user wrote: "{text}"\n'
        "Is this a food log entry (log) or a request for nutrition analytics over a period (report)?\n"
        "Answer with one word: log or report."
    )
    result = await ask_model(prompt, max_tokens=10)
    return "report" if "report" in result.lower() else "log"


async def parse_nutrition_correction(text: str) -> dict:
    """Parses a text nutrition correction → dict with only the mentioned fields."""
    prompt = (
        f'The user wants to correct a nutrition entry: "{text}"\n'
        "Return JSON with only the mentioned fields (do not include others):\n"
        "Allowed keys: calories, protein, fat, carbs\n"
        'Example: {"calories": 1200, "protein": 50}\n'
        "Return ONLY JSON."
    )
    raw = await ask_model(prompt, max_tokens=80)
    return _parse_llm_json(raw)


_MEDICAL_DOC_PROMPT = """This is a medical document. Determine the type and return JSON (no markdown).

IF this is a lab test with numeric values (blood, hormones, biochemistry, vitamins, tumor markers, etc.):
{"document_type": "lab", "test_date": "YYYY-MM-DD", "lab_name": "...", "notes": null,
 "markers": [
   {"parameter_name": "Glucose", "parameter_key": "glucose", "category": "biochemistry",
    "value_numeric": 5.2, "value_text": "5.2", "unit": "mmol/L",
    "ref_min": 4.1, "ref_max": 6.0, "ref_text": null, "is_abnormal": false},
   ...
 ]}

IF this is a doctor report with text (ultrasound, MRI, X-ray, mammography, cytology, smear):
{"document_type": "report", "study_date": "YYYY-MM-DD",
 "study_type": "uzi|mrt|rentgen|mammografia|cytology|other",
 "body_area": "...", "lab_name": "...", "equipment": "...", "doctor": "...",
 "description": "full protocol text", "conclusion": "final conclusion only"}

Categories for parameter_key (category):
- cbc: wbc, rbc, hemoglobin, hematocrit, mcv, mch, mchc, rdw, platelets, mpv, esr,
       neutrophils_pct, lymphocytes_pct, monocytes_pct, eosinophils_pct, basophils_pct,
       neutrophils_abs, lymphocytes_abs, monocytes_abs, eosinophils_abs, basophils_abs
- biochemistry: glucose, insulin, homa_ir, alt, ast, alkaline_phosphatase,
                bilirubin_total, bilirubin_direct, bilirubin_indirect, hba1c, creatinine, urea
- lipids: cholesterol, hdl, ldl, vldl, non_hdl, triglycerides, atherogenicity
- hormones: fsh, lh, estradiol, progesterone, testosterone, free_testosterone, shbg,
            free_testosterone_index, prolactin, dhea_s, cortisol, 17oh_progesterone
- thyroid: tsh, t3_free, t4_free
- vitamins: vitamin_d, vitamin_b12, folate, iron, ferritin, calcium, magnesium,
            phosphorus, zinc, selenium, copper, parathormone
- tumor_markers: ca125, he4, roma2, ca153, cea, afp

Rules:
- value_numeric — float, null if value is like "< 37" or "negative"
- value_text — always a string (as it appears in the document)
- is_abnormal — true if the result is outside the normal range (marked with * or outside ref_min..ref_max)
- test_date / study_date — sample collection / study date (ISO YYYY-MM-DD)
- For unknown parameters: parameter_key = transliteration in snake_case
Return ONLY JSON."""


async def analyze_medical_image(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """Vision LLM: parses a photo/screenshot of a medical document → structured dict."""
    models = [settings.vision_medical_model, settings.vision_food_model]
    last_err: Exception | None = None

    for m in models:
        try:
            text, _, _ = await _call_vision(m, image_bytes, media_type, _MEDICAL_DOC_PROMPT, 6000)
            return _parse_llm_json(text)
        except Exception as e:
            log.warning("analyze_medical_image via %s failed: %s", m, e)
            last_err = e

    raise RuntimeError(f"analyze_medical_image failed: {last_err}")


async def parse_medical_text(text: str) -> dict:
    """Text LLM: parses extracted text from a medical document → structured dict."""
    prompt = f"Medical document:\n\n{text}\n\n{_MEDICAL_DOC_PROMPT}"
    raw = await ask_model(prompt, model=settings.vision_medical_model, max_tokens=6000)
    return _parse_llm_json(raw)


_BODY_METRICS_PROMPT = """Analyze the photo or screenshot from a smart scale and return JSON strictly in this format (no markdown):
{"weight": 70.5, "body_fat_pct": 28.5, "muscle_kg": 45.2, "water_pct": 52.3, "visceral_fat": 8, "bone_mass_kg": 2.3, "bmr_kcal": 1450, "bmi": 24.5, "date": null}

Use the numbers exactly as shown on the screen. If a field is not on the screen — set it to null.
date — ISO date (YYYY-MM-DD) if visible on screen, otherwise null.
Return ONLY JSON, no explanations."""


async def analyze_body_metrics_image(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """Analyzes a smart scale photo via vision LLM → dict with body metrics."""
    models = [settings.vision_food_model, settings.vision_medical_model]
    last_err: Exception | None = None

    for m in models:
        try:
            text, _, _ = await _call_vision(m, image_bytes, media_type, _BODY_METRICS_PROMPT, 256)
            return _parse_llm_json(text)
        except Exception as e:
            log.warning("analyze_body_metrics_image via %s failed: %s", m, e)
            last_err = e

    raise RuntimeError(f"analyze_body_metrics_image failed: {last_err}")


_SLEEP_IMAGE_PROMPT = """The screenshot shows sleep data from Apple Health.
It can be:
- Summary screen (shows totals: Deep X hr Y min, REM ..., Core ..., Awake ...)
- Single-phase intervals screen (Core Intervals / Deep Intervals / REM Intervals / Awake Intervals)
- In Bed screen with total time in bed

Extract the data and return JSON:
{"sleep_date": "YYYY-MM-DD", "deep_min": 65, "rem_min": 103, "core_min": 172, "awake_min": 6,
 "in_bed_min": null, "total_min": 342, "bedtime_start": "23:00", "bedtime_end": "06:45"}

Rules:
- sleep_date — date of the night (e.g. "6-7 Mar 2026" → "2026-03-06"), null if not visible
- If the screen shows intervals for one phase — sum all intervals for that phase, others null
- If it is the summary screen — use the total values
- All values in MINUTES (convert hrs/min)
- bedtime_start/end — time in HH:MM format, null if not visible
- Do not include keys with null values if they are not on the screen
Return ONLY JSON."""


async def analyze_sleep_image(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """Parses a sleep screenshot from Apple Health → dict with phases in minutes."""
    models = [settings.vision_food_model, settings.vision_medical_model]
    last_err: Exception | None = None

    for m in models:
        try:
            text, _, _ = await _call_vision(m, image_bytes, media_type, _SLEEP_IMAGE_PROMPT, 256)
            return _parse_llm_json(text)
        except Exception as e:
            log.warning("analyze_sleep_image via %s failed: %s", m, e)
            last_err = e

    raise RuntimeError(f"analyze_sleep_image failed: {last_err}")


async def parse_body_measurements(text: str) -> dict:
    """Parses text body measurements → dict with fields in cm (and/or weight) and optional date."""
    today = date.today().isoformat()
    prompt = (
        f'Today is {today}. The user wrote body measurements: "{text}"\n'
        "Return JSON with only the mentioned fields (do not include others).\n"
        "Allowed keys: weight, arms_cm, thighs_cm, neck_cm, shin_cm, waist_cm, chest_cm, hips_cm, date\n"
        "weight — weight in kg, other measurements — centimeters, date — ISO date YYYY-MM-DD if mentioned (yesterday, February 20, etc.), otherwise omit.\n"
        'Example: {"arms_cm": 27, "thighs_cm": 51, "waist_cm": 74, "date": "2026-02-20"}\n'
        "Return ONLY JSON."
    )
    raw = await ask_model(prompt, max_tokens=150)
    return _parse_llm_json(raw)


async def parse_date_range(text: str) -> tuple[date, date]:
    """Parses a date range from text → (date_from, date_to)."""
    today = date.today()
    prompt = (
        f'Today is {today.isoformat()}. The user wrote: "{text}"\n'
        "Determine the period and return JSON strictly in this format (no markdown):\n"
        '{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}\n'
        "Return ONLY JSON."
    )
    raw = await ask_model(prompt, max_tokens=60)
    data = _parse_llm_json(raw)
    return date.fromisoformat(data["from"]), date.fromisoformat(data["to"])


async def ask_model_with_tools(
    prompt: str,
    tools: list[dict],
    tool_callables: dict[str, Callable],
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 2000,
    max_iterations: int = 10,
    force_tools: bool = False,
    history: list[dict] | None = None,
    return_model: bool = False,
) -> str | tuple[str, str]:
    """Agent loop: LLM calls tools until it returns final text.

    tools         — list of OpenAI tool-schemas ({"type":"function","function":{...}})
    tool_callables — {"tool_name": async_callable_that_returns_str}
    force_tools   — if True, first iteration uses tool_choice="required"
    model         — full LiteLLM model-string or short name. Default: settings.agent_model.
    """
    target = model or settings.agent_model

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    total_in = total_out = 0
    start = time.monotonic()

    for iteration in range(1, max_iterations + 1):
        tc = "required" if (force_tools and iteration == 1) else "auto"
        try:
            response = await litellm.acompletion(
                model=target,
                messages=messages,
                tools=tools,
                tool_choice=tc,
                max_tokens=max_tokens,
                timeout=90,
                num_retries=0,
            )
        except litellm.RateLimitError:
            # Do not fall back to cheap_model — Mini does not follow system prompt
            # and produces garbled responses. Better to surface the error.
            raise
        choice = response.choices[0]
        usage = response.usage
        total_in += usage.prompt_tokens
        total_out += usage.completion_tokens

        if choice.finish_reason == "stop" or not choice.message.tool_calls:
            elapsed = time.monotonic() - start
            _log_cost(target, total_in, total_out, elapsed)
            log.info("agent loop: iterations=%d in=%d out=%d time=%.2fs", iteration, total_in, total_out, elapsed)
            content = choice.message.content or ""
            if return_model:
                return content, model_label(target)
            return content

        # Serialize assistant response with tool_calls for the next request
        msg = choice.message
        assistant_dict: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_dict["tool_calls"] = [
                {
                    "id": tc_item.id,
                    "type": "function",
                    "function": {
                        "name": tc_item.function.name,
                        "arguments": tc_item.function.arguments,
                    },
                }
                for tc_item in msg.tool_calls
            ]
        messages.append(assistant_dict)

        # Execute all tool_calls
        for tc_item in msg.tool_calls:
            name = tc_item.function.name
            try:
                args = json.loads(tc_item.function.arguments)
            except json.JSONDecodeError:
                args = {}
            fn = tool_callables.get(name)
            if fn is None:
                result = f"Unknown tool: {name}"
            else:
                try:
                    result = await fn(**args)
                except Exception as exc:
                    log.error("Tool %s(%s) failed: %s", name, args, exc, exc_info=True)
                    result = f"Tool {name} error: {exc}"
            messages.append({
                "role": "tool",
                "tool_call_id": tc_item.id,
                "content": str(result),
            })

    raise RuntimeError(f"Agent loop exceeded max_iterations={max_iterations}")
