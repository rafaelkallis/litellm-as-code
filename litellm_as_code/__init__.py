"""litellm-as-code: declarative runtime-state management for a LiteLLM proxy.

LiteLLM-as-code reconciles the live runtime state of a LiteLLM proxy
(users, teams, team members, virtual API keys, credentials, models)
against a declarative YAML spec, applying only the deltas — the same
"desired state in a file" workflow as Terraform/HCL, but for the
LiteLLM admin REST API.
"""

__version__ = "0.2.0"
