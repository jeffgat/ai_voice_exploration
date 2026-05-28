"""Placeholder notes for a future Retell integration.

Retell can abstract much of the telephony and realtime voice layer. A later
adapter could receive lifecycle webhooks such as call_started,
transcript_updated, and call_ended, then map them into the same session store
and orchestration layer used by the browser simulation.

The main design goal is to keep LLM policy, fake tools, state, and logging
separate from the transport, so Retell or Twilio can be swapped in later.
"""

