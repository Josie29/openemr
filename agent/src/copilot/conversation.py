import time
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

from pydantic_ai.messages import ModelMessage

from copilot.ingestion.registry import DocumentFactRegistry
from copilot.retrieval import ChunkRegistry
from copilot.verification import FetchLog


@dataclass
class ConversationSession:
    """Server-side state for one multi-turn conversation, bound to a single patient.

    Holds the Pydantic AI ``message_history`` replayed into each turn, plus the three grounding
    registries *accumulated across the whole conversation*: the ``fetched`` FHIR log, the ``chunks``
    guideline registry, and the ``documents`` extracted-lab-fact registry. Accumulating all three is
    what lets a later turn cite a resource an earlier turn read, a guideline chunk an earlier turn
    retrieved, or a lab fact an earlier turn extracted — the grounding gate resolves claims against
    them (and the extraction cache means a follow-up need not re-OCR the same report).
    """

    patient_id: str
    messages: list[ModelMessage] = field(default_factory=list)
    fetched: FetchLog = field(default_factory=FetchLog)
    chunks: ChunkRegistry = field(default_factory=ChunkRegistry)
    documents: DocumentFactRegistry = field(default_factory=DocumentFactRegistry)
    last_used: float = 0.0


class ConversationStore:
    """In-memory, TTL-evicted store of conversations keyed by an opaque id.

    Deliberately process-local: conversation history contains PHI (tool results), so it stays in
    the agent service rather than round-tripping through the client. At multi-replica scale this
    needs a shared store (e.g. Redis) or sticky sessions — a documented scale boundary, not a
    concern for the current single-instance deploy. The clock is injected so tests can drive TTL
    deterministically.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_sessions: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessions: dict[str, ConversationSession] = {}
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._clock = clock

    def create(self, patient_id: str) -> tuple[str, ConversationSession]:
        """Open a new conversation bound to ``patient_id``.

        Args:
            patient_id: The FHIR patient logical id the conversation is scoped to.

        Returns:
            A ``(conversation_id, session)`` pair; the id is opaque (a uuid4 hex).
        """
        self._evict_if_full()
        conversation_id = uuid4().hex
        session = ConversationSession(patient_id=patient_id, last_used=self._clock())
        self._sessions[conversation_id] = session
        return conversation_id, session

    def get(self, conversation_id: str) -> ConversationSession | None:
        """Return a live session, or None if it is unknown or has expired.

        Expiry is lazy: an expired session is dropped on access. Reading a live session refreshes
        its ``last_used`` so an actively-used conversation is not evicted mid-thread.

        Args:
            conversation_id: The opaque conversation id from a prior turn's response.

        Returns:
            The session, or None when unknown/expired.
        """
        session = self._sessions.get(conversation_id)
        if session is None:
            return None
        now = self._clock()
        if now - session.last_used > self._ttl_seconds:
            del self._sessions[conversation_id]
            return None
        session.last_used = now
        return session

    def _evict_if_full(self) -> None:
        """Drop least-recently-used sessions while at capacity — a simple bound on memory."""
        while self._sessions and len(self._sessions) >= self._max_sessions:
            oldest = min(self._sessions, key=lambda cid: self._sessions[cid].last_used)
            del self._sessions[oldest]
