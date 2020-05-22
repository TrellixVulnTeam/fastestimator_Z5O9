# Copyright 2019 The FastEstimator Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import dis
import inspect
from collections import deque, namedtuple
from typing import Any, Callable, List, Mapping, Optional, Set, Tuple, Union

import numpy as np
import tensorflow as tf
import torch

from fastestimator.util.util import strip_prefix

_BoundFn = namedtuple('_BoundFn', ['func', 'args'])
_Command = namedtuple('_Command', ['left', 'right', 'command'])
_Condition = namedtuple('_Condition', ['left', 'right', 'condition'])
_VarWrap = namedtuple('_VarWrap', ['var'])
_CommandTable = {
    'POWER': '**',
    'MULTIPLY': '*',
    'MATRIX_MULTIPLY': '@',
    'FLOOR_DIVIDE': '//',
    'TRUE_DIVIDE': '/',
    'MODULO': '%',
    'ADD': '+',
    'SUBTRACT': '-',
    'SUBSCR': '[]',
    'LSHIFT': '<<',
    'RSHIFT': '>>',
    'AND': '&',
    'XOR': '^',
    'OR': '|',
    '>': '>',
    '<': '<',
    '==': '==',
    '!=': '!=',
    '<=': '<=',
    '>=': '>='
}


def _deref_is_callable(instruction: dis.Instruction, closure_vars: inspect.ClosureVars) -> bool:
    """A function to determine whether an `instruction` is referencing something that is callable or not.

    Args:
        instruction: The instruction to be investigated.
        closure_vars: The variables in the current scope.

    Returns:
        True iff the `instruction` is pointing to a callable object.
    """
    deref = closure_vars.nonlocals.get(
        instruction.argval,
        closure_vars.globals.get(instruction.argval, closure_vars.builtins.get(instruction.argval, None)))
    return hasattr(deref, '__call__')


def _trace_value(inp: Any, wrap_str: bool = True, include_id: bool = True) -> str:
    """Convert an input value to it's string representation.

    Args:
        inp: The input value to be converted.
        wrap_str: Whether literal string values should be wrapped inside extra quote marks.
        include_id: Whether to include object ids when representing objects.

    Returns:
        A string representation of the input.
    """
    if isinstance(inp, str):
        return f"'{inp}'" if wrap_str else inp
    elif hasattr(inp, '_fe_traceability_summary'):
        # The first time a traceable object goes through here it won't have it's summary instantiated yet, so it will
        # fall through to the class check at the end to get it's id.
        # noinspection PyProtectedMember
        return inp._fe_traceability_summary
    elif isinstance(inp, (int, float, bool, type(None))):
        return f"{inp}"
    elif inspect.ismethod(inp):
        return f"the '{inp.__name__}' method of ({_trace_value(inp.__self__, wrap_str, include_id)})"
    elif inspect.isfunction(inp) or inspect.isclass(inp):
        if inspect.isfunction(inp) and inp.__name__ == "<lambda>":
            code = inp.__code__
            var_names = code.co_varnames
            # Attempt to figure out what the lambda function is doing. If it is being used only to invoke some other
            # function (like one might do with LRScheduler), then the parse should work.
            instructions = [x for x in dis.get_instructions(code)]
            closure_vars = inspect.getclosurevars(inp)
            func_description = _parse_instructions(closure_vars, instructions)
            if func_description is None:
                return "a lambda function passing {}".format(
                    str.join(", ", map(lambda x: "'{}'".format(x), var_names)) or 'no arguments')
            else:
                return "a lambda function passing {} to: {}".format(
                    str.join(", ", map(lambda x: "'{}'".format(x), var_names)) or 'no arguments',
                    _trace_value(func_description, wrap_str, include_id))
        else:
            return f"{inp.__module__}.{inp.__qualname__}"
    elif isinstance(inp, _Command):
        return "{} {} {}".format(_trace_value(inp.left, wrap_str, include_id),
                                 inp.command,
                                 _trace_value(inp.right, wrap_str, include_id))
    elif isinstance(inp, _Condition):
        return "{} if {} else {}".format(_trace_value(inp.left, wrap_str, include_id),
                                         _trace_value(inp.condition, wrap_str, include_id),
                                         _trace_value(inp.right, wrap_str, include_id))
    elif isinstance(inp, _BoundFn):
        return "{} invoked with: {}".format(_trace_value(inp.func, wrap_str, include_id=False),
                                            _trace_value(inp.args, wrap_str=False, include_id=include_id))
    elif isinstance(inp, inspect.BoundArguments):
        return _trace_value(inp.arguments, wrap_str=False, include_id=include_id)
    elif isinstance(inp, _VarWrap):
        return inp.var
    elif isinstance(inp, (tf.keras.Model, torch.nn.Module)):
        name = inp.model_name if hasattr(inp, 'model_name') else "<Unknown Model Name>"
        id_str = f" (id: {id(inp)})" if include_id else ""
        return f"a neural network model named '{name}'{id_str}"
    # The collections need to go after _Command and _BoundFn
    elif isinstance(inp, List):
        return "[{}]".format(", ".join([_trace_value(x, wrap_str, include_id) for x in inp]))
    elif isinstance(inp, Tuple):
        return "({})".format(", ".join([_trace_value(x, wrap_str, include_id) for x in inp]))
    elif isinstance(inp, Set):
        return "{{{}}}".format(", ".join([_trace_value(x, wrap_str, include_id) for x in inp]))
    elif isinstance(inp, Mapping):
        return "{{{}}}".format(", ".join([
            "{}={}".format(_trace_value(k, wrap_str=wrap_str, include_id=include_id),
                           _trace_value(v, wrap_str=True, include_id=True)) for k,
            v in inp.items()
        ]))
    elif isinstance(inp, (tf.Tensor, torch.Tensor, np.ndarray)):
        if isinstance(inp, (tf.Tensor, torch.Tensor)):
            inp = inp.numpy()
        return f"{inp}"
    # This should be the last elif
    elif hasattr(inp, '__class__'):
        inp = inp.__class__
        id_str = f" (id: {id(inp)})" if include_id else ""
        return f"an instance of {inp.__module__}.{inp.__qualname__}{id_str}"
    else:
        id_str = f" with id: {id(inp)})" if include_id else ""
        return f"an object{id_str}"


def _parse_instructions(closure_vars: inspect.ClosureVars, instructions: List[dis.Instruction]) -> Optional[Any]:
    """Convert a list of bytecode instructions into an argument-based representation.

    The set of `instructions` is expected to have come from a lambda expression, which means that the set of bytecode
    instructions is limited compared to examining any possible function.

    Args:
        closure_vars: The variables defining the current scope.
        instructions: A set of instructions being executed.

    Returns:
        The arguments being used to invoke whatever function(s) are defined in the `instructions`, or None if parsing
        fails.
    """
    instructions = [x for x in instructions]  # Shallow copy to manipulate list
    conditions = []
    args = []
    idx = 0
    while idx < len(instructions):
        instruction = instructions[idx]
        if instruction.opname == 'RETURN_VALUE':
            # Lambda functions don't support the return keyword, instead values are returned implicitly
            if conditions:
                current_condition = conditions.pop()
                arg = args.pop()
                instructions.pop(idx - 1)
                idx -= 1
                # In lambda functions, conditions always fill in the order: condition -> left -> right
                if current_condition.left is None:
                    conditions.append(_Condition(left=arg, condition=current_condition.condition, right=None))
                    instructions.pop(idx - 1)
                    idx -= 1
                else:
                    args.append(
                        _Condition(left=current_condition.left, condition=current_condition.condition, right=arg))
                    if conditions:
                        # The return value can be used to satisfy a condition slot
                        idx -= 1
            else:
                break
        elif instruction.opname == 'LOAD_CONST':
            # It's a constant value
            args.append(instruction.argval)
        elif instruction.opname == 'LOAD_FAST':
            # It's a variable from a lambda expression
            args.append(_VarWrap(instruction.argval))
        elif instruction.opname in ('BUILD_LIST', 'BUILD_TUPLE', 'BUILD_SET'):
            # It's an iterable
            n_args = instruction.argval
            arg = deque()
            for i in range(n_args):
                arg.appendleft(args.pop())
                instructions.pop(idx - 1)
                idx -= 1
            arg = list(arg)
            if instruction.opname == 'BUILD_TUPLE':
                arg = tuple(arg)
            elif instruction.opname == 'BUILD_SET':
                arg = set(arg)
            args.append(arg)
        elif instruction.opname == "BUILD_MAP":
            # It's a map
            n_keys = instruction.argval
            arg = {}
            for i in range(n_keys):
                v = args.pop()
                k = args.pop()
                instructions.pop(idx - 1)
                idx -= 1
                instructions.pop(idx - 1)
                idx -= 1
                arg[k] = v
            args.append(arg)
        elif instruction.opname == "BUILD_CONST_KEY_MAP":
            # It's a map that had constant keys
            keys = args.pop()
            instructions.pop(idx - 1)
            idx -= 1
            vals = deque()
            for i in range(instruction.argval):
                vals.appendleft(args.pop())
                instructions.pop(idx - 1)
                idx -= 1
            args.append({key: val for key, val in zip(keys, vals)})
        elif instruction.opname == 'LOAD_DEREF' and not _deref_is_callable(
                instruction, closure_vars) and not instructions[idx + 1].opname in ('LOAD_METHOD', 'LOAD_ATTR'):
            # It's a reference to a variable that's not being used to invoke some other function
            args.append(
                closure_vars.nonlocals.get(
                    instruction.argval,
                    closure_vars.globals.get(instruction.argval, closure_vars.builtins.get(instruction.argval, None))))
        elif instruction.opname in ('LOAD_METHOD', 'LOAD_ATTR', 'LOAD_GLOBAL', 'LOAD_DEREF'):
            # We're setting up a function call, which may or may not be invoked
            # Look ahead to combine all of the function pieces together into 1 variable
            function = instructions[idx].argval
            function = closure_vars.nonlocals.get(
                function, closure_vars.globals.get(function, closure_vars.builtins.get(function, None)))
            while idx + 1 < len(instructions):
                if instructions[idx + 1].opname in ('LOAD_METHOD', 'LOAD_ATTR'):
                    function = getattr(function, instructions[idx + 1].argval)
                    instructions.pop(idx + 1)
                else:
                    break
            args.append(function)
        elif instruction.opname in ('CALL_METHOD', 'CALL_FUNCTION', 'CALL_FUNCTION_KW'):
            kwargs = {}
            kwarg_names = []
            if instruction.opname == 'CALL_FUNCTION_KW':
                # Gather the keywords, which were added with a LOAD_CONST call
                kwarg_names = args.pop()
                instructions.pop(idx - 1)
                idx -= 1
            # Gather the args
            n_args = instruction.argval
            fn_args = deque()
            for i in range(n_args):
                fn_args.appendleft(args.pop())
                instructions.pop(idx - 1)
                idx -= 1
            for name in reversed(kwarg_names):
                kwargs[name] = fn_args.pop()
            # Gather the fn
            function = args.pop()
            instructions.pop(idx - 1)  # Remove the method def from the stack
            idx -= 1
            # Bind the fn
            bound_args = inspect.signature(function).bind(*fn_args, **kwargs)
            bound_args.apply_defaults()
            args.append(_BoundFn(function, bound_args))
        elif instruction.opname.startswith('BINARY_') or instruction.opname.startswith(
                'INPLACE_') or instruction.opname == 'COMPARE_OP':
            # Capture actual inline function stuff like: 0.5 + x
            command = strip_prefix(strip_prefix(instruction.opname, 'BINARY_'), 'INPLACE_')
            if instruction.opname == 'COMPARE_OP':
                command = instruction.argval
            if command not in _CommandTable:
                return None  # We weren't able to parse this correctly
            right = args.pop()
            instructions.pop(idx - 1)
            idx -= 1
            left = args.pop()
            instructions.pop(idx - 1)
            idx -= 1
            args.append(_Command(left, right, _CommandTable[command]))
        elif instruction.opname == 'POP_JUMP_IF_FALSE':
            # a if a < b else b     |||     <left> if <condition> else <right>
            conditions.append(_Condition(left=None, right=None, condition=args.pop()))
            instructions.pop(idx - 1)
            idx -= 1
        else:
            # TODO - to be fully rigorous we need the rest: https://docs.python.org/3.7/library/dis.html#bytecodes
            # TODO - LIST_APPEND, SET_ADD, MAP_ADD, BUILD_STRING, CALL_FUNCTION_EX, BUILD_TUPLE_UNPACK, etc.
            # Note that this function is only ever used to examine lambda functions, which helps to restrict the set of
            # possible commands
            return None  # We weren't able to parse this correctly
        idx += 1
    # Return the bound args
    if conditions or len(args) != 1:
        return None  # We weren't able to parse this correctly
    return args[0]


def traceable(whitelist: Union[str, Tuple[str]] = (), blacklist: Union[str, Tuple[str]] = ()) -> Callable:
    """A decorator to be placed on classes in order to make them traceable and to enable a deep restore.

    Decorated classes will gain the .fe_summary() and .fe_state() methods.

    Args:
        whitelist: Arguments which should be included in a deep restore of the decorated class.
        blacklist: Arguments which should be excluded from a deep restore of the decorated class.

    Returns:
        The decorated class.
    """
    if isinstance(whitelist, str):
        whitelist = (whitelist, )
    if isinstance(blacklist, str):
        blacklist = (blacklist, )
    if whitelist and blacklist:
        raise ValueError("Traceable objects may specify a whitelist or a blacklist, but not both")

    def make_traceable(cls):
        base_init = getattr(cls, '__init__')

        def init(self, *args, **kwargs):
            if not hasattr(self, '_fe_state_whitelist'):
                self._fe_state_whitelist = whitelist
            if not hasattr(self, '_fe_state_blacklist'):
                self._fe_state_blacklist = blacklist + ('_fe_state_whitelist', '_fe_state_blacklist')
            if not hasattr(self, '_fe_traceability_summary'):
                bound_args = inspect.signature(base_init).bind(self, *args, **kwargs)
                bound_args.apply_defaults()
                bound_args = bound_args.arguments
                bound_args.pop('self')
                self._fe_traceability_summary = _trace_value(_BoundFn(self, bound_args))
            base_init(self, *args, **kwargs)

        setattr(cls, '__init__', init)

        def fe_summary(self) -> str:
            """Return a summary of how this class was instantiated (for traceability).

            Args:
                self: The bound class instance.

            Returns:
                A summary of the instance.
            """
            return f"This experiment used {self._fe_traceability_summary}"

        base_func = getattr(cls, 'fe_summary', None)
        if base_func is None:
            setattr(cls, 'fe_summary', fe_summary)

        def fe_state(self) -> Mapping[str, Any]:
            """Return a summary of this class' state variables (for deep restore).

            Args:
                self: The bound class instance.

            Returns:
                The state variables to be considered for deep restore.
            """
            state_dict = {key: value for key, value in self.__dict__.items()}
            if self._fe_state_whitelist:
                state_dict = {key: state_dict[key] for key in whitelist}
            for key in self._fe_state_blacklist:
                state_dict.pop(key, None)
            return state_dict

        base_func = getattr(cls, 'fe_state', None)
        # If the user specified a whitelist or blacklist then use the default impl
        if whitelist or blacklist or base_func is None:
            setattr(cls, 'fe_state', fe_state)

        return cls

    return make_traceable
