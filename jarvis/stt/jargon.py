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

# Curated subset for Deepgram Nova-3's `keyterm` runtime biasing. Deepgram
# docs warn accuracy degrades past ~30-50 keyterms (overfitting), so this
# is intentionally focused on the highest-leverage terms — file extensions
# the user actually says ("dot go", ".go"), Endor-specific identifiers,
# and project nouns that Azure regularly fumbles. Keep ordering by
# perceived importance; Deepgram may use position as a soft signal.
DEEPGRAM_KEYTERMS = [
    # File extensions — most common STT failure mode (period gets dropped)
    ".go", ".py", ".ts", ".tsx", ".proto", ".yaml", ".json", ".md",
    "dot go", "dot py", "dot ts", "dot proto",
    # Endor-specific identifiers heard often
    "endorctl", "Endor Labs", "monorepo",
    "datawarehouseserver", "exporter",
    # Tooling user touches daily
    "Pipecat", "Cartesia", "Anthropic", "Claude Code",
    "Bazel", "BigQuery", "protobuf",
    # Programming languages
    "Go", "Python", "TypeScript",
    # Cloud / infra terms that often get mangled
    "Kubernetes", "ArgoCD", "Terraform", "GitHub Actions",
    "kubectl", "helm",
]
