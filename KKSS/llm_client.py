"""
llm_client.py

Connects to OpenAI's ChatGPT API and sends it the prompt built by
employee_recommendation_prompt.py, then parses the JSON recommendation
back into a Python dict.

SETUP (one-time):
    1. pip install openai
    2. Get an API key from https://platform.openai.com/api-keys
    3. Set it as an environment variable (recommended — never hardcode it):

       macOS/Linux:
           export OPENAI_API_KEY="sk-...your-key-here..."

       Windows (PowerShell):
           setx OPENAI_API_KEY "sk-...your-key-here..."

    4. Restart your terminal / IDE so the env variable is picked up.

USAGE (from main.py):
    from data_loader import load_employee_dataset
    from employee_recommendation_prompt import build_employee_recommendation_prompt
    from llm_client import get_employee_recommendation

    df = load_employee_dataset("employee_dataset_simple.csv")
    prompt = build_employee_recommendation_prompt(
        problem_statement="Repair the electrical system of Train 215",
        problem_rating=8,
        employee_df=df,
        excluded_names=["John Tan"],
    )
    result = get_employee_recommendation(prompt)
    print(result["recommended_employees"])
"""

from __future__ import annotations

import json
import os
from typing import Optional

from openai import OpenAI, OpenAIError

DEFAULT_MODEL = "gpt-4o-mini"  # cheap + fast; use "gpt-4o" for higher quality


class LLMClientError(Exception):
    """Raised when the OpenAI API call fails or returns something we can't use."""


def get_employee_recommendation(
    prompt: str,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
) -> dict:
    """
    Send the recommendation prompt to ChatGPT and parse the JSON response.

    Args:
        prompt: the full prompt string built by
            employee_recommendation_prompt.build_employee_recommendation_prompt().
        api_key: OpenAI API key. If not provided, reads from the
            OPENAI_API_KEY environment variable.
        model: which OpenAI model to use. Defaults to "gpt-4o-mini".

    Returns:
        A dict with the shape:
            {
              "recommended_employees": [
                  {"employee_id": ..., "employee_name": ..., "job_role": ...,
                   "experience_level": ..., "reason": ...},
                  ...
              ],
              "notes": "..."
            }

    Raises:
        LLMClientError: if the API key is missing, the API call fails, or
            the model's response isn't valid JSON in the expected shape.
    """
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise LLMClientError(
            "No OpenAI API key found. Pass api_key=... or set the "
            "OPENAI_API_KEY environment variable."
        )

    client = OpenAI(api_key=key)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an AI maintenance scheduling assistant. "
                        "Always respond with valid JSON only, matching exactly "
                        "the format requested in the user's message. No markdown "
                        "code fences, no extra commentary."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,  # low temperature: we want consistent, rule-following output
            response_format={"type": "json_object"},  # forces valid JSON output
        )
    except OpenAIError as exc:
        raise LLMClientError(f"OpenAI API call failed: {exc}") from exc

    raw_text = response.choices[0].message.content

    if not raw_text or not raw_text.strip():
        raise LLMClientError("OpenAI returned an empty response.")

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise LLMClientError(
            f"OpenAI response was not valid JSON: {exc}\nRaw response: {raw_text}"
        ) from exc

    if "recommended_employees" not in parsed:
        raise LLMClientError(
            f"OpenAI response is missing 'recommended_employees' key. Got: {parsed}"
        )

    return parsed


if __name__ == "__main__":
    # Manual smoke test — requires OPENAI_API_KEY to be set and network access.
    from data_loader import load_employee_dataset
    from employee_recommendation_prompt import build_employee_recommendation_prompt

    df = load_employee_dataset("employee_dataset_simple.csv")
    test_prompt = build_employee_recommendation_prompt(
        problem_statement="Repair the electrical system of Train 215",
        problem_rating=8,
        employee_df=df,
    )

    result = get_employee_recommendation(test_prompt)
    print(json.dumps(result, indent=2))