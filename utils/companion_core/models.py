"""Strict, framework-independent durable companion reflection contracts."""

from __future__ import annotations

import re
import json
from typing import Literal, Annotated
import ipaddress
from urllib.parse import unquote, urlsplit
from collections.abc import Mapping, Collection

from pydantic import Field, BaseModel, ConfigDict, AfterValidator, ValidationError, model_validator

ConversationMode = Literal["normal", "companion"]

MAX_REFLECTION_CHARS = 65536
MAX_IDENTIFIER_CHARS = 64
MAX_MEMBER_REF_CHARS = 96
MAX_LIFE_STATE_CHARS = 6000
_CAPABILITY_RE = re.compile(
    r"(?i)(?:(?<![a-z0-9])(?:media_ref|image_ref|attachment_ref|participant_ref|"
    r"session_ref|channel_ref|account_ref|execution_ref|message_ref|lease_token|"
    r"access_token|refresh_token|cookie_ref)(?![a-z0-9])|"
    r"\b(?:local|attachment|file|blob|data):|\bauthorization\s*[:=])"
)
_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+")
_BINARY_RE = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_=-]{64,}(?![A-Za-z0-9+/_=-])")
_BASE64_RE = re.compile(r"(?i)(?:;\s*base64\s*,|\bbase64\s*[:=]\s*[A-Za-z0-9+/_=-]{32,})")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_FENCE_RE = re.compile(r"\A```(?:json)?[ \t]*\r?\n(.*)\r?\n```[ \t]*\Z", re.DOTALL | re.IGNORECASE)


class ReflectionValidationError(ValueError):
    """The provider output is not a complete, grounded durable reflection."""


def _durable_text(value: str) -> str:
    if value and not value.strip():
        raise ValueError("durable text must not be whitespace-only")
    if _CONTROL_RE.search(value):
        raise ValueError("durable text contains control or invalid Unicode characters")
    if _CAPABILITY_RE.search(value):
        raise ValueError("durable text must not contain ephemeral capabilities or credentials")
    if _BASE64_RE.search(value):
        raise ValueError("durable text must not contain base64 payloads")
    for match in _BINARY_RE.finditer(value):
        token = match.group()
        # Long, diverse opaque tokens are binary/capability material rather
        # than the concise prose this state stores. Plain repeated words do
        # not become binary merely because the text reaches its length bound.
        if len(set(token)) >= 12 or any(character in token for character in "+/=_"):
            raise ValueError("durable text must not contain encoded binary payloads")
    for match in _URL_RE.finditer(value):
        url = match.group().rstrip(".,;!?)\u3002\uff0c\uff1b\uff01\uff09")
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("durable text contains an invalid URL") from exc
        if parsed.scheme.lower() not in {"http", "https"} or not host:
            raise ValueError("durable text must not contain private resource URLs")
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("durable text must not contain authenticated or transient URLs")
        if port is not None and port not in {80, 443}:
            raise ValueError("durable text must not contain private service URLs")
        normalized = host.lower().rstrip(".")
        path = parsed.path
        for _ in range(3):
            decoded = unquote(path)
            if decoded == path:
                break
            path = decoded
        path = path.lower()
        if re.search(r"/p/[a-z0-9_-]{20,}(?:/|$)", path):
            raise ValueError("durable text must not contain bearer preview capabilities")
        if re.search(r"/(?:private|attachments?|uploads?|gchatpic_new|offpic_new)(?:/|$)", path):
            raise ValueError("durable text must not contain private media URLs")
        if normalized.endswith((".qpic.cn", ".discordapp.com", ".discordapp.net")):
            raise ValueError("durable text must not contain transient platform media URLs")
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            if "." not in normalized or normalized.endswith(
                (".localhost", ".local", ".internal", ".home", ".lan", ".test", ".invalid")
            ):
                raise ValueError("durable text must not contain non-public URLs")
            # Reject ambiguous numeric host spellings accepted by some HTTP
            # stacks as loopback (integer, shortened IPv4, or hexadecimal).
            if re.fullmatch(r"[0-9.]+", normalized) or normalized.startswith("0x"):
                raise ValueError("durable text must not contain ambiguous address URLs")
        else:
            if not address.is_global:
                raise ValueError("durable text must not contain non-public address URLs")
    return value


def _identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError("identifier must be a stable ASCII identifier")
    return value


class _FrozenList(list):
    """Preserve the list/JSON-array ABI without mutable snapshot contents."""

    def _readonly(self, *args, **kwargs):
        raise TypeError("companion state collections are immutable")

    __setitem__ = _readonly
    __delitem__ = _readonly
    __iadd__ = _readonly
    __imul__ = _readonly
    append = _readonly
    clear = _readonly
    extend = _readonly
    insert = _readonly
    pop = _readonly
    remove = _readonly
    reverse = _readonly
    sort = _readonly

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


def _immutable_list(value: list) -> list:
    return _FrozenList(value)


DurableText = Annotated[str, Field(strict=True), AfterValidator(_durable_text)]
StateKey = Annotated[DurableText, Field(min_length=1, max_length=MAX_IDENTIFIER_CHARS), AfterValidator(_identifier)]
MemberRef = Annotated[DurableText, Field(min_length=1, max_length=MAX_MEMBER_REF_CHARS), AfterValidator(_identifier)]
EvidenceTurnId = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]
Evidence = Annotated[list[EvidenceTurnId], Field(max_length=16), AfterValidator(_immutable_list)]


class _DurableModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always", validate_default=True
    )


class LifePlan(_DurableModel):
    key: StateKey
    title: Annotated[DurableText, Field(min_length=1, max_length=160)]
    status: Literal["planned", "active", "paused", "completed"]
    progress: Annotated[DurableText, Field(max_length=600)]


class LifeEvent(_DurableModel):
    key: StateKey
    summary: Annotated[DurableText, Field(min_length=1, max_length=600)]
    source: Literal["fictional"] = "fictional"


class LifeState(_DurableModel):
    focus: Annotated[DurableText, Field(max_length=500)] = ""
    interests: Annotated[
        list[Annotated[DurableText, Field(min_length=1, max_length=120)]],
        Field(max_length=12),
        AfterValidator(_immutable_list),
    ] = Field(default_factory=list)
    plans: Annotated[list[LifePlan], Field(max_length=12), AfterValidator(_immutable_list)] = Field(
        default_factory=list
    )
    recent_events: Annotated[list[LifeEvent], Field(max_length=16), AfterValidator(_immutable_list)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def unique_keys(self) -> LifeState:
        if len({plan.key for plan in self.plans}) != len(self.plans):
            raise ValueError("life plans contain duplicate keys")
        if len({event.key for event in self.recent_events}) != len(self.recent_events):
            raise ValueError("life events contain duplicate keys")
        if len(set(self.interests)) != len(self.interests):
            raise ValueError("life interests contain duplicates")
        encoded = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded) > MAX_LIFE_STATE_CHARS:
            raise ValueError(f"serialized life state must not exceed {MAX_LIFE_STATE_CHARS} characters")
        return self


class AttentionUpdate(_DurableModel):
    member_ref: MemberRef
    key: StateKey
    kind: Literal["concern", "topic", "connection"]
    summary: Annotated[DurableText, Field(min_length=1, max_length=800)]
    status: Literal["open", "waiting_reply", "dormant", "resolved"]
    evidence_turn_ids: Evidence = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_evidence(self) -> AttentionUpdate:
        if len(set(self.evidence_turn_ids)) != len(self.evidence_turn_ids):
            raise ValueError("attention contains duplicate evidence turn IDs")
        return self


class ContactProposal(_DurableModel):
    member_ref: MemberRef
    key: StateKey
    purpose: Literal["check_in", "share", "continue", "free_chat"]
    topic: Annotated[DurableText, Field(min_length=1, max_length=400)]
    reason: Annotated[DurableText, Field(min_length=1, max_length=400)]
    evidence_turn_ids: Evidence = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_evidence(self) -> ContactProposal:
        if len(set(self.evidence_turn_ids)) != len(self.evidence_turn_ids):
            raise ValueError("contact contains duplicate evidence turn IDs")
        return self


class ReflectionProposal(_DurableModel):
    life: LifeState
    attention: Annotated[list[AttentionUpdate], Field(max_length=24), AfterValidator(_immutable_list)] = Field(
        default_factory=list
    )
    contact: ContactProposal | None = None
    review_after_minutes: Annotated[int, Field(strict=True, ge=10, le=1440)] = 60

    @model_validator(mode="after")
    def unique_attention(self) -> ReflectionProposal:
        if len({(entry.member_ref, entry.key) for entry in self.attention}) != len(self.attention):
            raise ValueError("attention contains duplicate member/key pairs")
        return self


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReflectionValidationError("reflection JSON contains a duplicate object field")
        result[key] = value
    return result


def _json_constant(value: str) -> object:
    raise ReflectionValidationError("reflection JSON contains a nonfinite number")


def _complete_object(value: object, fields: Collection[str], *, label: str) -> None:
    if not isinstance(value, dict) or not set(fields).issubset(value):
        raise ReflectionValidationError(f"{label} must contain every declared JSON field")


def _complete_output(value: object) -> None:
    _complete_object(value, ReflectionProposal.model_fields, label="reflection")
    life = value["life"]
    _complete_object(life, LifeState.model_fields, label="life")
    for field, model in (("plans", LifePlan), ("recent_events", LifeEvent)):
        entries = life[field]
        if isinstance(entries, list):
            for entry in entries:
                _complete_object(entry, model.model_fields, label=field)
    attention = value["attention"]
    if isinstance(attention, list):
        for entry in attention:
            _complete_object(entry, AttentionUpdate.model_fields, label="attention item")
    if value["contact"] is not None:
        _complete_object(value["contact"], ContactProposal.model_fields, label="contact")


def _turn_set(values: Collection[int], *, label: str) -> set[int]:
    if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Collection):
        raise ReflectionValidationError(f"{label} must be a collection of positive turn IDs")
    if any(type(value) is not int or not 1 <= value <= 9223372036854775807 for value in values):
        raise ReflectionValidationError(f"{label} must contain only positive integer turn IDs")
    return set(values)


def parse_reflection(
    text: str,
    *,
    member_refs: Collection[str],
    turn_ids: Collection[int],
    member_turn_ids: Mapping[str, Collection[int]] | None = None,
) -> ReflectionProposal:
    """Parse a complete response and fence all identities against host evidence.

    The optional ownership map strengthens the flat evidence allowlist. The
    store must still revalidate ownership against its completed-chat ledger
    before persistence, including when this parser is bypassed by a caller.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_REFLECTION_CHARS:
        raise ReflectionValidationError("reflection must be nonempty bounded JSON text")
    if isinstance(member_refs, (str, bytes, Mapping)) or not isinstance(member_refs, Collection):
        raise ReflectionValidationError("member_refs must be a collection of host-known identifiers")
    if any(
        not isinstance(ref, str) or not 1 <= len(ref) <= MAX_MEMBER_REF_CHARS or not _IDENTIFIER_RE.fullmatch(ref)
        for ref in member_refs
    ):
        raise ReflectionValidationError("member_refs contains an invalid host identifier")
    members = set(member_refs)
    known_turns = _turn_set(turn_ids, label="turn_ids")
    owned_turns: dict[str, set[int]] | None = None
    if member_turn_ids is not None:
        if not isinstance(member_turn_ids, Mapping) or set(member_turn_ids) - members:
            raise ReflectionValidationError("member_turn_ids contains an unknown member")
        owned_turns = {}
        assigned_turns: set[int] = set()
        for ref, values in member_turn_ids.items():
            owned = _turn_set(values, label="member_turn_ids")
            if not owned.issubset(known_turns):
                raise ReflectionValidationError("member_turn_ids contains evidence outside turn_ids")
            if assigned_turns.intersection(owned):
                raise ReflectionValidationError("member_turn_ids assigns the same evidence to multiple members")
            assigned_turns.update(owned)
            owned_turns[ref] = owned
    candidate = text.strip()
    if candidate.startswith("```"):
        match = _FENCE_RE.fullmatch(candidate)
        if match is None:
            raise ReflectionValidationError("reflection must contain one complete JSON fence without surrounding prose")
        candidate = match.group(1)
    try:
        payload = json.loads(candidate, object_pairs_hook=_json_object, parse_constant=_json_constant)
        _complete_output(payload)
        proposal = ReflectionProposal.model_validate(payload)
    except ReflectionValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = exc.errors(include_input=False, include_url=False)
            # Extra-field names may themselves contain private capabilities;
            # diagnostics must not copy rejected provider material into audit.
            detail = "; ".join(error["msg"] for error in errors[:8])
            raise ReflectionValidationError(f"invalid reflection structure: {detail}") from exc
        raise ReflectionValidationError("reflection is not valid complete JSON") from exc
    updates = list(proposal.attention)
    if proposal.contact is not None:
        updates.append(proposal.contact)
    for update in updates:
        if update.member_ref not in members:
            raise ReflectionValidationError("reflection references an unknown member")
        evidence = set(update.evidence_turn_ids)
        if not evidence.issubset(known_turns):
            raise ReflectionValidationError("reflection references unknown evidence turn IDs")
        if owned_turns is not None and not evidence.issubset(owned_turns.get(update.member_ref, set())):
            raise ReflectionValidationError("reflection evidence belongs to a different member")
        if isinstance(update, AttentionUpdate) and not evidence:
            raise ReflectionValidationError("attention requires real member-owned evidence")
        if isinstance(update, ContactProposal) and update.purpose in {"check_in", "continue"} and not evidence:
            raise ReflectionValidationError("check_in and continue contacts require real member-owned evidence")
    return proposal
