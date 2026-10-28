"""Generation-local, host-owned identities and bounded companion member context."""

from __future__ import annotations

from copy import deepcopy
import json
from secrets import token_hex
from datetime import UTC, datetime
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Mapping, Iterator, Sequence

from ..config import LLMChatConfig
from ..identity import ChatIdentity
from ..core.types import ChatMessage
from ..core.user_turns import derive_speaker_id, update_user_payload, companion_history_payload
from ..core.agent_trace import AgentTurnRecorder
from ..relationships.state import load_relationship_snapshot
from ..persona.memory_context import load_memory_context

_MAX_PARTICIPANTS = 64
_MAX_PROFILE_CHARS = 1200
_MAX_MEMORY_CHARS = 1200
_MAX_RELATIONSHIP_CHARS = 1600


def _bounded(value: object, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _scope(session: object) -> tuple[str, str, str]:
    account = getattr(session, "account")
    return (str(account.platform), str(account.self_id), str(getattr(session, "channel").id))


@dataclass(slots=True)
class _ParticipantInput:
    session: object
    identity: ChatIdentity
    own_text: str
    model_text: str
    directed: bool
    message_id: str
    reply_to_ref: str
    received_at: datetime | None
    context: dict[str, object]


class CompanionParticipantRegistry:
    """Bind real input events, never identities inferred from model-visible text.

    This registry supplies read-only context. It never mutates AgentAccessContext,
    root authorization, or any member's profile/relationship state.
    """

    def __init__(
        self,
        config: LLMChatConfig,
        root_session: object,
        root_identity: ChatIdentity | None,
    ) -> None:
        self.config = config
        self.root_session = root_session
        self.root_identity = root_identity
        self.scope = _scope(root_session)
        self.scope_ref = ""
        self.closed = False
        self._inputs: dict[int, _ParticipantInput] = {}
        self._contexts: dict[str, dict[str, object]] = {}
        self._engaged: set[str] = set()
        self._confirmed_engaged: set[str] = set()
        self._recorded: set[int] = set()

    def bind_scope(self, scope_ref: str) -> None:
        if self.scope_ref and self.scope_ref != scope_ref:
            raise ValueError("Companion participant scope cannot change")
        self.scope_ref = scope_ref

    def _check(self, session: object) -> None:
        if self.closed or _scope(session) != self.scope:
            raise ValueError("Companion participant input is outside its live scope")

    async def register_input(
        self,
        session: object,
        identity: ChatIdentity,
        *,
        own_text: str,
        directed: bool = True,
        model_text: str | None = None,
        message_id: str | None = None,
        received_at: datetime | None = None,
    ) -> dict[str, object]:
        self._check(session)
        if not identity.user_id:
            raise ValueError("A participant input requires an actual member identity")
        if session.event is self.root_session.event and (self.root_identity is None or identity != self.root_identity):
            raise ValueError("A companion input cannot replace its root authority")
        existing = self._inputs.get(id(session.event))
        if existing is not None:
            if existing.identity != identity or existing.own_text != own_text or existing.directed != bool(directed):
                raise ValueError("A companion input cannot change its host-bound author or evidence")
            return self.participant_context(session)
        if identity.user_id not in self._contexts:
            if len(self._contexts) >= _MAX_PARTICIPANTS:
                raise ValueError("Companion batch participant capacity exceeded")
            relation, snapshot = await load_relationship_snapshot(identity.user_id, self.scope[2])
            memory = await load_memory_context(self.config, identity.user_id, self.scope[2], own_text)
            self._contexts[identity.user_id] = {
                "profile": _bounded(json.dumps(memory.chat_profile, ensure_ascii=False), _MAX_PROFILE_CHARS),
                "relationship": {
                    "axes": snapshot["axes"],
                    "description": _bounded(snapshot["description"], _MAX_RELATIONSHIP_CHARS // 2),
                    "impression": _bounded(relation.impression, _MAX_RELATIONSHIP_CHARS // 2),
                    "emotions": [
                        {key: emotion[key] for key in ("name", "intensity", "cause") if key in emotion}
                        for emotion in snapshot["emotions"]
                        if isinstance(emotion, Mapping)
                    ],
                },
                "relevant_memories": _bounded("\n".join(memory.relevant_memories), _MAX_MEMORY_CHARS),
                "read_only": True,
            }
            # Emotion arrays can contain old, long evidence fields. Bound the
            # rendered view without modifying the stored snapshot or provenance.
            emotions = self._contexts[identity.user_id]["relationship"]["emotions"]
            self._contexts[identity.user_id]["relationship"]["emotions"] = _bounded(
                json.dumps(emotions, ensure_ascii=False), _MAX_RELATIONSHIP_CHARS
            )
        event_message = getattr(session.event, "message", None)
        self._inputs[id(session.event)] = _ParticipantInput(
            session=session,
            identity=identity,
            own_text=own_text,
            model_text=own_text if model_text is None else model_text,
            directed=bool(directed),
            message_id=str(getattr(event_message, "id", "") or "") if message_id is None else message_id,
            reply_to_ref=f"message_{token_hex(16)}",
            received_at=received_at,
            context=self._contexts[identity.user_id],
        )
        return self.participant_context(session)

    def has_input(self, session: object) -> bool:
        self._check(session)
        return id(session.event) in self._inputs

    def host_author(self, session: object) -> dict[str, object]:
        self._check(session)
        item = self._inputs[id(session.event)]
        if not self.scope_ref:
            raise ValueError("Companion participant scope is not bound")
        return {
            "version": 1,
            "user_id": item.identity.user_id,
            "speaker_id": derive_speaker_id(self.scope_ref, item.identity.user_id),
            "display_name": _bounded(item.identity.display_name, 160),
            "participant_ref": item.identity.participant_ref,
            "source_message_id": item.message_id,
            "directed": item.directed,
            "evidence_eligible": item.directed,
        }

    def resolve_reply(self, session: object, reference: str) -> str:
        """Resolve only a real input already applied within this live generation."""
        self._check(session)
        for event_id, item in self._inputs.items():
            if item.reply_to_ref == reference and event_id in self._recorded and item.message_id:
                return item.message_id
        raise ValueError("Reply target is not an applied message in this conversation")

    def current_user_is_unambiguous(self) -> bool:
        return self.root_identity is not None and all(
            item.identity.user_id == self.root_identity.user_id
            for event_id, item in self._inputs.items()
            if event_id in self._recorded
        )

    def participant_context(self, session: object) -> dict[str, object]:
        self._check(session)
        item = self._inputs[id(session.event)]
        return {"display_name": _bounded(item.identity.display_name, 160), **deepcopy(item.context)}

    def snapshot(self) -> list[dict[str, object]]:
        if not self.scope_ref:
            raise ValueError("Companion participant scope is not bound")
        seen: set[str] = set()
        result = []
        for item in self._inputs.values():
            if item.identity.user_id in seen:
                continue
            seen.add(item.identity.user_id)
            result.append(
                {
                    "speaker_id": derive_speaker_id(self.scope_ref, item.identity.user_id),
                    **self.participant_context(item.session),
                }
            )
        return result

    def attribute_message(self, session: object, message: ChatMessage) -> ChatMessage:
        author = self.host_author(session)
        # Drop any author/profile claim in serialized user data, then overwrite
        # it from the exact registered event. No field here grants tool access.
        return update_user_payload(
            message,
            lambda payload: {
                **companion_history_payload(payload),
                "speaker": author["display_name"],
                "speaker_id": author["speaker_id"],
                "participant_ref": author["participant_ref"],
                "reply_to_ref": self._inputs[id(session.event)].reply_to_ref,
                "companion_participant_context": self.participant_context(session),
            },
        )

    def record_input(
        self,
        recorder: AgentTurnRecorder,
        session: object,
        content: str,
        *,
        fresh_context: bool = False,
        attachments: Sequence[Mapping[str, object]] = (),
        created_at: datetime | None = None,
        steer_sequence: int | None = None,
    ) -> None:
        author = self.host_author(session)
        if id(session.event) in self._recorded:
            return
        item = self._inputs[id(session.event)]
        timestamp = created_at or item.received_at
        if timestamp is not None and timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(UTC).replace(tzinfo=None)
        recorder.append(
            "user_input",
            role="user",
            payload={
                "content": content,
                "speaker": author["display_name"],
                "fresh_context": fresh_context,
                "input_format": "structured_user_turn",
                "host_author": author,
                "own_text": item.own_text,
                "attachments": list(attachments),
                **(
                    {
                        "steer_sequence": steer_sequence,
                        "received_at": item.received_at.isoformat() if item.received_at is not None else None,
                        "applied_at": created_at.isoformat() if created_at is not None else None,
                    }
                    if steer_sequence is not None
                    else {}
                ),
            },
            status="applied" if steer_sequence is not None else "",
            created_at=timestamp,
        )
        self._recorded.add(id(session.event))

    def directed(self, session: object) -> bool:
        self._check(session)
        return self._inputs[id(session.event)].directed

    def engage(self, speaker_ids: Sequence[str]) -> None:
        if self.closed or not self.scope_ref:
            raise ValueError("Companion engagement requires a live bound registry")
        if not isinstance(speaker_ids, (list, tuple)):
            raise ValueError("Companion engagement requires a list of speaker IDs")
        authors = {
            derive_speaker_id(self.scope_ref, item.identity.user_id): item.identity.user_id
            for item in self._inputs.values()
            if id(item.session.event) in self._recorded
        }
        if len(speaker_ids) > len(authors):
            raise ValueError("Companion engagement exceeds admitted participant capacity")
        if any(not isinstance(speaker, str) or speaker not in authors for speaker in speaker_ids):
            raise ValueError("Companion engagement must name admitted actual speakers")
        if len(set(speaker_ids)) != len(speaker_ids):
            raise ValueError("Companion engagement must not repeat a speaker")
        self._engaged.update(authors[speaker] for speaker in speaker_ids)

    def participation_payload(self, *, confirmed: bool) -> dict[str, object]:
        self._confirmed_engaged = set(self._engaged) if confirmed else set()
        members = []
        for user_id in sorted(self._confirmed_engaged):
            sources = [
                item
                for item in self._inputs.values()
                if item.identity.user_id == user_id and id(item.session.event) in self._recorded
            ]
            members.append(
                {
                    "user_id": user_id,
                    "speaker_id": derive_speaker_id(self.scope_ref, user_id),
                    "source_message_ids": list(dict.fromkeys(item.message_id for item in sources)),
                }
            )
        return {"version": 1, "confirmed": bool(confirmed), "participants": members}

    def evidence_members(self) -> tuple[tuple[object, ChatIdentity], ...]:
        """Return actual directed or confirmed-engaged members for feedback."""
        members: dict[str, tuple[object, ChatIdentity]] = {}
        for item in self._inputs.values():
            if id(item.session.event) not in self._recorded:
                continue
            if item.directed or item.identity.user_id in self._confirmed_engaged:
                members[item.identity.user_id] = (item.session, item.identity)
        return tuple(members.values())

    def root_is_directed(self) -> bool:
        item = self._inputs.get(id(self.root_session.event))
        return item is not None and item.directed


_CURRENT: ContextVar[CompanionParticipantRegistry | None] = ContextVar("companion_participants", default=None)


def current_companion_participants() -> CompanionParticipantRegistry | None:
    registry = _CURRENT.get()
    return registry if registry is not None and not registry.closed else None


@contextmanager
def companion_participants_scope(registry: CompanionParticipantRegistry) -> Iterator[CompanionParticipantRegistry]:
    if registry.closed:
        raise ValueError("A closed participant registry cannot be reused")
    token = _CURRENT.set(registry)
    try:
        yield registry
    finally:
        registry.closed = True
        _CURRENT.reset(token)
