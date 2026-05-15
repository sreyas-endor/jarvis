"""Jargon phrase list — single source of truth for both STT backends.

Used by Azure phrase-list STT (acoustic-model biasing) and Whisper
(LM-prompt biasing). Put highest-value terms early; Whisper truncates
past ~224 tokens, Azure phrase lists have no equivalent cap.
"""

JARGON_PHRASES = [
    "Claude", "Claude Code", "Anthropic", "Pipecat", "Cartesia",
    "Whisper", "MLX", "ElevenLabs", "OpenAI", "Cursor",
    "monorepo", "Endor Labs", "kubectl", "Bazel", "Helm",
    "Docker", "Kubernetes", "ArgoCD", "Terraform",
    "GitHub", "GitLab", "Slack", "Jira", "Atlassian", "Confluence",
    "Linear", "Notion",
    "Python", "TypeScript", "JavaScript", "Go",
    "Azure", "GCP", "AWS", "BigQuery", "MongoDB",
    "gRPC", "protobuf", "REST", "SQL",
    "LLM", "STT", "TTS", "MCP", "SDK", "CLI", "PR", "CI/CD",
    "npm", "yarn", "uv", "pip", "git", "ssh", "vim", "VSCode",
]

WHISPER_INITIAL_PROMPT = (
    "This is a casual conversation with a developer. Common terms include "
    + ", ".join(JARGON_PHRASES) + "."
)
