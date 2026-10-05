"""Voice: the wake word, and staying in the conversation after it.

Speech-to-text and text-to-speech are separate concerns with their own provider
seams; this package is only about WHEN the assistant is listening and why.
"""

from .conversation_mode import ConversationMode, Mode
from .wake import Wake, WakeDetector

__all__ = ["ConversationMode", "Mode", "Wake", "WakeDetector"]
