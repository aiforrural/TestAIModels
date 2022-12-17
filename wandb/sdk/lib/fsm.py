#!/usr/bin/env python
"""Finite state machine.

Simple FSM implementation.

Usage:
    ```python
    class A:
        def on_output(self, inputs) -> None:
            pass

    class B:
        def on_output(self, inputs) -> None:
            pass

    def to_b(inputs) -> bool:
        return True

    def to_a(inputs) -> bool:
        return True

    f = Fsm(states=[A(), B()],
            table={A: [(to_b, B)],
                   B: [(to_a, A)]})
    f.run({"input1": 1, "input2": 2})
    ```
"""

import sys
from abc import abstractmethod
from dataclasses import dataclass
from typing import Dict, Generic, Optional, Sequence, Type, TypeVar, Union

if sys.version_info >= (3, 8):
    from typing import Protocol, runtime_checkable
else:
    from typing_extensions import Protocol, runtime_checkable

if sys.version_info >= (3, 10):
    from typing import TypeAlias
else:
    from typing_extensions import TypeAlias

T_FsmInputs = TypeVar("T_FsmInputs", contravariant=True)


@runtime_checkable
class FsmStateOutput(Protocol[T_FsmInputs]):
    @abstractmethod
    def on_state(self, inputs: T_FsmInputs) -> None:
        ...  # pragma: no cover


@runtime_checkable
class FsmStateEnter(Protocol[T_FsmInputs]):
    @abstractmethod
    def on_enter(self, inputs: T_FsmInputs) -> None:
        ...  # pragma: no cover


@runtime_checkable
class FsmStateExit(Protocol[T_FsmInputs]):
    @abstractmethod
    def on_exit(self, inputs: T_FsmInputs) -> None:
        ...  # pragma: no cover


FsmState: TypeAlias = Union[
    FsmStateOutput[T_FsmInputs], FsmStateEnter[T_FsmInputs], FsmStateExit[T_FsmInputs]
]


class FsmCondition(Protocol[T_FsmInputs]):
    def __call__(self, inputs: T_FsmInputs) -> bool:
        ...  # pragma: no cover


class FsmAction(Protocol[T_FsmInputs]):
    def __call__(self, inputs: T_FsmInputs) -> None:
        ...  # pragma: no cover


@dataclass
class FsmEntry(Generic[T_FsmInputs]):
    condition: FsmCondition[T_FsmInputs]
    target_state: Type[FsmState[T_FsmInputs]]
    action: Optional[FsmAction[T_FsmInputs]] = None


FsmTable: TypeAlias = Dict[Type[FsmState[T_FsmInputs]], Sequence[FsmEntry[T_FsmInputs]]]


class Fsm(Generic[T_FsmInputs]):
    _state_dict: Dict[Type[FsmState], FsmState]
    _table: FsmTable[T_FsmInputs]
    _state: FsmState[T_FsmInputs]

    def __init__(
        self, states: Sequence[FsmState], table: FsmTable[T_FsmInputs]
    ) -> None:
        self._state_dict = {type(s): s for s in states}
        self._table = table
        self._state = self._state_dict[type(states[0])]

    def _transition(
        self,
        inputs: T_FsmInputs,
        new_state: Type[FsmState[T_FsmInputs]],
        action: Optional[FsmAction[T_FsmInputs]],
    ) -> None:
        if isinstance(self._state, FsmStateExit):
            # print("ON_EXIT")
            self._state.on_exit(inputs)

        # print("NEWSTATE:", new_state)
        prev_state = type(self._state)
        self._state = self._state_dict[new_state]

        if action:
            action(inputs)

        if prev_state != new_state:
            if isinstance(self._state, FsmStateEnter):
                # print("ON_ENTER")
                self._state.on_enter(inputs)

    def _check_transitions(self, inputs: T_FsmInputs) -> None:
        for entry in self._table[type(self._state)]:
            if entry.condition(inputs):
                self._transition(inputs, entry.target_state, entry.action)
                return

    def input(self, inputs: T_FsmInputs) -> None:
        # print("R1", self._state, inputs)
        self._check_transitions(inputs)
        # print("R2", self._state, inputs)
        if isinstance(self._state, FsmStateOutput):
            self._state.on_state(inputs)