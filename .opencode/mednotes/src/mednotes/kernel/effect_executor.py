from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from mednotes.kernel.effects import WorkflowEffect, WorkflowEffectKind, WorkflowEffectResult


class MissingWorkflowEffectAdapter(ValueError):
    """Raised when an FSM emitted an effect that has no official adapter."""


@dataclass(frozen=True)
class WorkflowEffectExecutionContext:
    dry_run: bool = False
    environment: dict[str, str] = field(default_factory=dict)
    artifacts_dir: str = ""


class WorkflowEffectAdapter(Protocol):
    def run(self, effect: WorkflowEffect, context: WorkflowEffectExecutionContext) -> WorkflowEffectResult:
        """Materialize one already-authorized workflow effect."""


@dataclass
class WorkflowEffectExecutor:
    adapters: dict[WorkflowEffectKind, WorkflowEffectAdapter]
    context: WorkflowEffectExecutionContext = field(default_factory=WorkflowEffectExecutionContext)

    def execute(
        self,
        effect: WorkflowEffect,
        *,
        context: WorkflowEffectExecutionContext | None = None,
    ) -> WorkflowEffectResult:
        if not isinstance(effect, WorkflowEffect):
            raise TypeError("WorkflowEffectExecutor.execute requires WorkflowEffect")
        adapter = self._adapter_for(effect.kind)
        return adapter.run(effect, context or self.context)

    def _adapter_for(self, kind: WorkflowEffectKind) -> WorkflowEffectAdapter:
        match kind:
            case (
                WorkflowEffectKind.RUN_SUBWORKFLOW
                | WorkflowEffectKind.CALL_SPECIALIST_MODEL
                | WorkflowEffectKind.ASK_HUMAN
                | WorkflowEffectKind.WAIT_EXTERNAL
            ):
                return self._required(kind)

    def _required(self, kind: WorkflowEffectKind) -> WorkflowEffectAdapter:
        adapter = self.adapters.get(kind)
        if adapter is None:
            raise MissingWorkflowEffectAdapter(f"no adapter registered for workflow effect kind: {kind.value}")
        return adapter
