# Engagement question drafting

Status: experimental and disabled. The Engagement Hub flag and a separate AI
flag must both be enabled. A deployment must first register and authorize its
own gateway consumer; the development tests use a synthetic provider only.

- **Workflow:** an Organizer supplies text and explicitly agrees to send it to
  the configured gateway. The gateway returns suggested single-choice questions
  with exact source excerpts. An Organizer reviews and optionally copies the
  suggestions into an unsaved activity draft. Saving and publication are
  separate authorized actions.
- **Model interface:** one OpenAI-compatible SDK call using server-controlled
  `TM_LLM_GATEWAY_URL`, `TM_LLM_GATEWAY_KEY`, and `tier/mid`. No model can
  invoke tools or execute event commands. There is no direct-provider fallback.
- **Framework:** a single bounded call and deterministic validation need no
  graph or checkpoint; no workflow starter is copied. There is no autonomous
  agent or model-selected tool.
- **Boundaries:** server-side Organizer authorization, current workspace and
  profile context are checked, including after the model returns. Input is at
  most 8,000 characters; output is at most five questions and 1,300 model
  tokens. The call has a 30-second timeout, zero automatic retries and shares
  the existing process concurrency limit. The gateway must enforce its own
  consumer budget and rate limits across instances.
- **Data and validation:** only the text supplied for this operation is sent;
  credentials are never included. Model output is untrusted, schema-validated,
  and every evidence excerpt must match the supplied source exactly. Matching
  excerpts are not proof that a proposed answer is correct; human review is
  mandatory. The draft and evidence stay private to authorized event staff.
- **Recovery and qualification:** there is no model-side publication or write
  to replay. An interrupted generation can be repeated deliberately. Before
  activation, qualify the named gateway consumer, route, key, locality, budget,
  latency, errors, injection resistance and content handling with synthetic
  examples; complete independent security review and the product release gates.

The general event product continues to work when the gateway is unavailable.
