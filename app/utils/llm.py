from openai import OpenAI
import time

def get_response(
        client: OpenAI,
        system_prompt: str,
        user_prompt: str,
        model: str = "gpt-5-nano",
        verbosity: str = "low",
        effort: str = "low",
        web_search: bool = True,
) -> str:
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "developer",
                "content": [
                    {
                    "type": "input_text",
                    "text": system_prompt
                    }
                ]
            },
            {
                "role": "user",
                "content": [
                    {
                    "type": "input_text",
                    "text": user_prompt
                    }
                ]
            }
        ],
        text={
            "format": {
                "type": "text"
            },
            "verbosity": verbosity
        },
        reasoning={
            "effort": effort,
            "summary": "auto"
        },
        tools=[
                {
                    "type": "web_search",
                    "user_location": {
                        "type": "approximate"
                    },
                    "search_context_size": "medium"
                }
            ] if web_search else [],
        store=True,
        include=[
            "reasoning.encrypted_content",
            "web_search_call.action.sources"
        ]
        )
    
    return str(response.output_text)