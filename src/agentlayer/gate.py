"""The commit gate: irreversible actions wait for a person.

Approval travels outside the model's tool calls, through ``agentlayer gate approve``. If an agent
could approve its own request, text on a page could talk it into doing so.

This is defence in depth, not a guarantee: a custom button that calls an API directly and has
an innocent name will not be recognised.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from enum import StrEnum

from agentlayer.errors import COMMIT_DENIED, COMMIT_GATED, NOT_FOUND, LayerError
from agentlayer.model import CommitFacts, Node, Ref, Role, SurfaceId

POLICY_ENV = "AGENTLAYER_COMMIT"

_LATIN_COMMIT = re.compile(
    r"\b(submit|send|pay|purchase|buy|place order|checkout|check out|delete|remove|publish"
    r"|transfer|withdraw|apply(?! filter)|confirm|authori[sz]e|e-?sign|sign here)\b",
    re.IGNORECASE,
)
_CJK_COMMIT = (
    "送出", "提交", "寄出", "發送", "付款", "購買", "下單", "結帳", "刪除", "移除", "申請", "確認",
    "发送", "删除",
)
_STEP = re.compile(r"\b(next|continue|back|previous|search|filter|close|cancel)\b", re.IGNORECASE)
_CJK_STEP = ("下一步", "上一步", "繼續", "搜尋", "取消", "關閉")
_PRESSABLE = frozenset({Role.BUTTON, Role.LINK, Role.MENUITEM, Role.TEXT, Role.OTHER, Role.GROUP})


class Policy(StrEnum):
    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


class GateStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    USED = "used"


@dataclass
class GateRequest:
    id: str
    agent: str
    surface: SurfaceId
    ref: Ref | None
    action: str
    what: str
    status: GateStatus
    created: float


def commit_reason(node: Node, facts: CommitFacts) -> str | None:
    """Why pressing this element commits something, or None when it does not."""
    if node.role not in _PRESSABLE or facts.search_form:
        return None
    name = node.name
    if _LATIN_COMMIT.search(name) or any(word in name for word in _CJK_COMMIT):
        return "its label names a commit"
    if _STEP.search(name) or any(word in name for word in _CJK_STEP):
        return None
    return "it submits a form" if facts.submit_button else None


class Gate:
    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or Policy(os.environ.get(POLICY_ENV, Policy.ASK.value))
        self._requests: dict[str, GateRequest] = {}
        self._next = 1

    def require(
        self,
        *,
        agent: str,
        surface: SurfaceId,
        ref: Ref | None,
        action: str,
        what: str,
        token: str | None,
    ) -> None:
        """Return only when this commit may go ahead. Consumes an approved token."""
        if self.policy is Policy.ALLOW:
            return
        if self.policy is Policy.DENY:
            raise LayerError(COMMIT_DENIED, f"commits are denied by policy: {what}")
        if token is not None and self._consume(token, agent, surface, ref, action):
            return
        request = self._file(agent, surface, ref, action, what)
        raise LayerError(
            COMMIT_GATED,
            f"gate={request.id} waits for a person: {what}",
            hint=(
                f"tell the person, who runs `agentlayer gate approve {request.id}`; "
                f"then repeat this call with gate={request.id}"
            ),
        )

    def pending(self) -> list[GateRequest]:
        return [r for r in self._requests.values() if r.status is GateStatus.PENDING]

    def approve(self, gate_id: str) -> GateRequest:
        return self._decide(gate_id, GateStatus.APPROVED)

    def reject(self, gate_id: str) -> GateRequest:
        return self._decide(gate_id, GateStatus.REJECTED)

    def _decide(self, gate_id: str, status: GateStatus) -> GateRequest:
        request = self._requests.get(gate_id)
        if request is None or request.status is not GateStatus.PENDING:
            raise LayerError(NOT_FOUND, f"no pending gate named {gate_id}")
        request.status = status
        return request

    def _file(
        self, agent: str, surface: SurfaceId, ref: Ref | None, action: str, what: str
    ) -> GateRequest:
        for existing in self._requests.values():
            same = (existing.agent, existing.surface, existing.ref, existing.action)
            if existing.status is GateStatus.PENDING and same == (agent, surface, ref, action):
                return existing
        gate_id = f"g{self._next}"
        self._next += 1
        request = GateRequest(
            gate_id, agent, surface, ref, action, what, GateStatus.PENDING, time.time()
        )
        self._requests[gate_id] = request
        return request

    def _consume(
        self, token: str, agent: str, surface: SurfaceId, ref: Ref | None, action: str
    ) -> bool:
        request = self._requests.get(token)
        if request is None or request.status is not GateStatus.APPROVED:
            return False
        if (request.agent, request.surface, request.ref, request.action) != (
            agent, surface, ref, action
        ):
            return False
        request.status = GateStatus.USED
        return True
