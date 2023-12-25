import inspect
from enum import Enum
from functools import cached_property
from itertools import chain
from keyword import iskeyword
from typing import (
    AbstractSet,
    Any,
    Dict,
    Generic,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)

from . import abi
from ._abi_types import (
    Type,
    decode_args,
    dispatch_type,
    dispatch_types,
    encode_args,
    keccak,
)
from ._entities import LogEntry, LogTopic
from ._provider import JSON


# We are using the `inspect` machinery to bind arguments to parameters.
# From Py3.11 on it does not allow parameter names to coincide with keywords,
# so we have to escape them.
# This can be avoided if we write our own `inspect.Signature` implementation.
def make_name_safe(name: str) -> str:
    if iskeyword(name):
        return name + "_"
    else:
        return name


class Signature:
    """Generalized signature of either inputs or outputs of a method."""

    def __init__(self, parameters: Union[Mapping[str, Type], Sequence[Type]]):
        if isinstance(parameters, Mapping):
            self._signature = inspect.Signature(
                parameters=[
                    inspect.Parameter(make_name_safe(name), inspect.Parameter.POSITIONAL_OR_KEYWORD)
                    for name, tp in parameters.items()
                ]
            )
            self._types = list(parameters.values())
            self._named_parameters = True
        else:
            self._signature = inspect.Signature(
                parameters=[
                    inspect.Parameter(f"_{i}", inspect.Parameter.POSITIONAL_ONLY)
                    for i in range(len(parameters))
                ]
            )
            self._types = list(parameters)
            self._named_parameters = False

    @property
    def empty(self) -> bool:
        return not bool(self._types)

    @cached_property
    def canonical_form(self) -> str:
        """Returns the signature serialized in the canonical form as a string."""
        return "(" + ",".join(tp.canonical_form for tp in self._types) + ")"

    def encode(self, *args: Any, **kwargs: Any) -> bytes:
        """
        Encodes assorted positional/keyword arguments into the bytestring
        according to the ABI format.
        """
        bound_args = self._signature.bind(*args, **kwargs)
        return encode_args(*zip(self._types, bound_args.args))

    def decode_into_tuple(self, value_bytes: bytes) -> Tuple[Any, ...]:
        """Decodes the packed bytestring into a list of values."""
        return decode_args(self._types, value_bytes)

    def decode_into_dict(self, value_bytes: bytes) -> Dict[str, Any]:
        """Decodes the packed bytestring into a dict of values."""
        decoded = self.decode_into_tuple(value_bytes)
        return dict(zip(self._signature.parameters, decoded))

    def __str__(self) -> str:
        if self._named_parameters:
            params = ", ".join(
                f"{tp.canonical_form} {name}"
                for name, tp in zip(self._signature.parameters, self._types)
            )
        else:
            params = ", ".join(f"{tp.canonical_form}" for tp in self._types)
        return f"({params})"


class Either:
    """Denotes an `OR` operation when filtering events."""

    def __init__(self, *items: Any):
        self.items = items


class EventSignature:
    """A signature representing the constructor of an event (that is, its fields)."""

    def __init__(self, parameters: Mapping[str, Type], indexed: AbstractSet[str]):
        parameters = {make_name_safe(name): val for name, val in parameters.items()}
        indexed = {make_name_safe(name) for name in indexed}
        self._signature = inspect.Signature(
            parameters=[
                inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                for name, tp in parameters.items()
                if name in indexed
            ]
        )
        self._types = parameters
        self._types_nonindexed = {
            name: self._types[name] for name in parameters if name not in indexed
        }
        self._indexed = indexed

    def encode_to_topics(
        self, *args: Any, **kwargs: Any
    ) -> Tuple[Optional[Tuple[bytes, ...]], ...]:
        """
        Binds given arguments to event's indexed parameters
        and encodes them as log topics.
        """

        bound_args = self._signature.bind_partial(*args, **kwargs)

        encoded_topics: List[Optional[Tuple[bytes, ...]]] = []
        for param_name in self._signature.parameters:
            if param_name not in bound_args.arguments:
                encoded_topics.append(None)
                continue

            bound_val = bound_args.arguments[param_name]
            tp = self._types[param_name]

            if isinstance(bound_val, Either):
                encoded_val = tuple(tp.encode_to_topic(elem) for elem in bound_val.items)
            else:
                # Make it a one-element tuple to simplify type signatures.
                encoded_val = (tp.encode_to_topic(bound_val),)

            encoded_topics.append(encoded_val)

        # remove trailing `None`s - they are redundant
        while encoded_topics and encoded_topics[-1] is None:
            encoded_topics.pop()

        return tuple(encoded_topics)

    def decode_log_entry(self, topics: Sequence[bytes], data: bytes) -> Dict[str, Any]:
        """Decodes the event fields from the given log entry data."""
        if len(topics) != len(self._indexed):
            raise ValueError(
                f"The number of topics in the log entry ({len(topics)}) does not match "
                f"the number of indexed fields in the event ({len(self._indexed)})"
            )

        decoded_topics = {
            name: self._types[name].decode_from_topic(topic)
            for name, topic in zip(self._signature.parameters, topics)
        }

        decoded_data_tuple = decode_args(self._types_nonindexed.values(), data)
        decoded_data = dict(zip(self._types_nonindexed, decoded_data_tuple))

        result = {}
        for name in self._types:
            if name in decoded_topics:
                result[name] = decoded_topics[name]
            else:
                result[name] = decoded_data[name]

        return result

    @cached_property
    def canonical_form(self) -> str:
        """Returns the signature serialized in the canonical form as a string."""
        return "(" + ",".join(tp.canonical_form for tp in self._types.values()) + ")"

    @cached_property
    def canonical_form_nonindexed(self) -> str:
        """Returns the signature serialized in the canonical form as a string."""
        return "(" + ",".join(tp.canonical_form for tp in self._types_nonindexed.values()) + ")"

    def __str__(self) -> str:
        params = []
        for name, tp in self._types.items():
            indexed = "indexed " if name in self._indexed else ""
            params.append(f"{tp.canonical_form} {indexed}{name}")
        return "(" + ", ".join(params) + ")"


class Constructor:
    """
    Contract constructor.

    .. note::

       If the name of a parameter given to the constructor matches a Python keyword,
       ``_`` will be appended to it.
    """

    inputs: Signature
    """Input signature."""

    payable: bool
    """Whether this method is marked as payable"""

    @classmethod
    def from_json(cls, method_entry: Dict[str, Any]) -> "Constructor":
        """Creates this object from a JSON ABI method entry."""
        if method_entry["type"] != "constructor":
            raise ValueError(
                "Constructor object must be created from a JSON entry with type='constructor'"
            )
        if "name" in method_entry:
            raise ValueError("Constructor's JSON entry cannot have a `name`")
        if "outputs" in method_entry and method_entry["outputs"]:
            raise ValueError("Constructor's JSON entry cannot have non-empty `outputs`")
        if method_entry["stateMutability"] not in ("nonpayable", "payable"):
            raise ValueError(
                "Constructor's JSON entry state mutability must be `nonpayable` or `payable`"
            )
        inputs = dispatch_types(method_entry.get("inputs", []))
        payable = method_entry["stateMutability"] == "payable"
        return cls(inputs, payable=payable)

    def __init__(self, inputs: Union[Mapping[str, Type], Sequence[Type]], payable: bool = False):
        self.inputs = Signature(inputs)
        self.payable = payable

    def __call__(self, *args: Any, **kwargs: Any) -> "ConstructorCall":
        """Returns an encoded call with given arguments."""
        input_bytes = self.inputs.encode(*args, **kwargs)
        return ConstructorCall(input_bytes)

    def __str__(self) -> str:
        return f"constructor{self.inputs} " + ("payable" if self.payable else "nonpayable")


class Mutability(Enum):
    """Possible states of a contract's method mutability."""

    PURE = "pure"
    """Solidity's ``pure`` (does not read or write the contract state)."""
    VIEW = "view"
    """Solidity's ``view`` (may read the contract state)."""
    NONPAYABLE = "nonpayable"
    """Solidity's ``nonpayable`` (may write the contract state)."""
    PAYABLE = "payable"
    """
    Solidity's ``payable`` (may write the contract state
    and accept associated funds with transactions).
    """

    @classmethod
    def from_json(cls, entry: str) -> "Mutability":
        values = dict(
            pure=Mutability.PURE,
            view=Mutability.VIEW,
            nonpayable=Mutability.NONPAYABLE,
            payable=Mutability.PAYABLE,
        )
        if entry not in values:
            raise ValueError(f"Unknown mutability identifier: {entry}")
        return values[entry]

    @property
    def payable(self) -> bool:
        return self == Mutability.PAYABLE

    @property
    def mutating(self) -> bool:
        return self == Mutability.PAYABLE or self == Mutability.NONPAYABLE


class Method:
    """
    A contract method.

    .. note::

       If the name of a parameter (input or output) given to the constructor
       matches a Python keyword, ``_`` will be appended to it.
    """

    outputs: Signature
    """Method's output signature."""

    payable: bool
    """Whether this method is marked as payable."""

    mutating: bool
    """Whether this method may mutate the contract state."""

    @classmethod
    def from_json(cls, method_entry: Dict[str, Any]) -> "Method":
        """Creates this object from a JSON ABI method entry."""
        if method_entry["type"] != "function":
            raise ValueError("Method object must be created from a JSON entry with type='function'")

        name = method_entry["name"]
        inputs = dispatch_types(method_entry["inputs"])

        mutability = Mutability.from_json(method_entry["stateMutability"])

        # Outputs can be anonymous
        outputs: Union[Dict[str, Type], List[Type]]
        if "outputs" not in method_entry:
            outputs = []
        elif all(entry["name"] == "" for entry in method_entry["outputs"]):
            outputs = [dispatch_type(entry) for entry in method_entry["outputs"]]
        else:
            outputs = dispatch_types(method_entry["outputs"])

        return cls(name=name, inputs=inputs, outputs=outputs, mutability=mutability)

    def __init__(
        self,
        name: str,
        mutability: Mutability,
        inputs: Union[Mapping[str, Type], Sequence[Type]],
        outputs: Union[Mapping[str, Type], Sequence[Type], Type, None] = None,
    ):
        self._name = name
        self._inputs = Signature(inputs)
        self._mutability = mutability
        self.payable = mutability.payable
        self.mutating = mutability.mutating

        if outputs is None:
            outputs = []

        if isinstance(outputs, Type):
            outputs = [outputs]
            self._single_output = True
        else:
            self._single_output = False

        self.outputs = Signature(outputs)

    @property
    def name(self) -> str:
        return self._name

    @property
    def inputs(self) -> Signature:
        return self._inputs

    def __call__(self, *args: Any, **kwargs: Any) -> "MethodCall":
        """Returns an encoded call with given arguments."""
        return MethodCall(self._encode_call(*args, **kwargs))

    @cached_property
    def selector(self) -> bytes:
        """Method's selector."""
        return keccak(self.name.encode() + self.inputs.canonical_form.encode())[:4]

    def _encode_call(self, *args: Any, **kwargs: Any) -> bytes:
        input_bytes = self.inputs.encode(*args, **kwargs)
        return self.selector + input_bytes

    def decode_output(self, output_bytes: bytes) -> Any:
        """Decodes the output from ABI-packed bytes."""
        results = self.outputs.decode_into_tuple(output_bytes)
        if self._single_output:
            results = results[0]
        return results

    def __str__(self) -> str:
        if self.outputs.empty:
            returns = ""
        else:
            returns = f" returns {self.outputs}"
        return f"function {self.name}{self.inputs} {self._mutability.value}{returns}"


class Event:
    """
    A contract event.

    .. note::

       If the name of a field given to the constructor matches a Python keyword,
       ``_`` will be appended to it.
    """

    @classmethod
    def from_json(cls, event_entry: Dict[str, Any]) -> "Event":
        """Creates this object from a JSON ABI method entry."""
        if event_entry["type"] != "event":
            raise ValueError("Event object must be created from a JSON entry with type='event'")

        name = event_entry["name"]
        fields = dispatch_types(event_entry["inputs"])
        if isinstance(fields, list):
            raise ValueError("Event fields must be named")

        indexed = {input_["name"] for input_ in event_entry["inputs"] if input_["indexed"]}

        return cls(name=name, fields=fields, indexed=indexed, anonymous=event_entry["anonymous"])

    def __init__(
        self,
        name: str,
        fields: Mapping[str, Type],
        indexed: AbstractSet[str],
        anonymous: bool = False,
    ):
        if anonymous and len(indexed) > 4:
            raise ValueError("Anonymous events can have at most 4 indexed fields")
        if not anonymous and len(indexed) > 3:
            raise ValueError("Non-anonymous events can have at most 3 indexed fields")

        self.name = name
        self.indexed = indexed
        self.fields = EventSignature(fields, indexed)
        self.anonymous = anonymous

    @cached_property
    def _topic(self) -> LogTopic:
        """The topic representing this event's signature."""
        return LogTopic(keccak(self.name.encode() + self.fields.canonical_form.encode()))

    def __call__(self, *args: Any, **kwargs: Any) -> "EventFilter":
        """
        Creates an event filter from provided values for indexed parameters.
        Some arguments can be omitted, which will mean that the filter
        will match events with any value of that parameter.
        :py:class:`Either` can be used to denote an OR operation and match
        either of several values of a parameter.
        """

        encoded_topics = self.fields.encode_to_topics(*args, **kwargs)

        log_topics: List[Optional[Tuple[LogTopic, ...]]] = []
        if not self.anonymous:
            log_topics.append((self._topic,))
        for topic in encoded_topics:
            if topic is None:
                log_topics.append(None)
            else:
                log_topics.append(tuple(LogTopic(elem) for elem in topic))

        return EventFilter(tuple(log_topics))

    def decode_log_entry(self, log_entry: LogEntry) -> Dict[str, Any]:
        """
        Decodes the event fields from the given log entry.
        Fields that cannot be decoded (indexed reference types,
        which are hashed before saving them to the log) are set to ``None``.
        """
        topics = log_entry.topics
        if not self.anonymous:
            if topics[0] != self._topic:
                raise ValueError("This log entry belongs to a different event")
            topics = topics[1:]

        return self.fields.decode_log_entry([bytes(topic) for topic in topics], log_entry.data)

    def __str__(self) -> str:
        return f"event {self.name}{self.fields}" + (" anonymous" if self.anonymous else "")


class EventFilter:
    """A filter for events coming from any contract address."""

    topics: Tuple[Optional[Tuple[LogTopic, ...]], ...]

    def __init__(self, topics: Tuple[Optional[Tuple[LogTopic, ...]], ...]):
        self.topics = topics


class Error:
    """A custom contract error."""

    @classmethod
    def from_json(cls, error_entry: Dict[str, Any]) -> "Error":
        """Creates this object from a JSON ABI method entry."""
        if error_entry["type"] != "error":
            raise ValueError("Error object must be created from a JSON entry with type='error'")

        name = error_entry["name"]
        fields = dispatch_types(error_entry["inputs"])
        if isinstance(fields, list):
            raise ValueError("Error fields must be named")

        return cls(name=name, fields=fields)

    def __init__(
        self,
        name: str,
        fields: Mapping[str, Type],
    ):
        self.name = name
        self.fields = Signature(fields)

    @cached_property
    def selector(self) -> bytes:
        """Error's selector."""
        return keccak(self.name.encode() + self.fields.canonical_form.encode())[:4]

    def decode_fields(self, data_bytes: bytes) -> Dict[str, Any]:
        """Decodes the error fields from the given packed data."""
        return self.fields.decode_into_dict(data_bytes)

    def __str__(self) -> str:
        return f"error {self.name}{self.fields}"


class Fallback:
    """A fallback method."""

    payable: bool
    """Whether this method is marked as payable"""

    @classmethod
    def from_json(cls, method_entry: Dict[str, Any]) -> "Fallback":
        """Creates this object from a JSON ABI method entry."""
        if method_entry["type"] != "fallback":
            raise ValueError(
                "Fallback object must be created from a JSON entry with type='fallback'"
            )
        if method_entry["stateMutability"] not in ("nonpayable", "payable"):
            raise ValueError(
                "Fallback method's JSON entry state mutability must be `nonpayable` or `payable`"
            )
        payable = method_entry["stateMutability"] == "payable"
        return cls(payable)

    def __init__(self, payable: bool = False):
        self.payable = payable

    def __str__(self) -> str:
        return "fallback() " + ("payable" if self.payable else "nonpayable")


class Receive:
    """A receive method."""

    payable: bool
    """Whether this method is marked as payable"""

    @classmethod
    def from_json(cls, method_entry: Dict[str, Any]) -> "Receive":
        """Creates this object from a JSON ABI method entry."""
        if method_entry["type"] != "receive":
            raise ValueError(
                "Receive object must be created from a JSON entry with type='fallback'"
            )
        if method_entry["stateMutability"] not in ("nonpayable", "payable"):
            raise ValueError(
                "Receive method's JSON entry state mutability must be `nonpayable` or `payable`"
            )
        payable = method_entry["stateMutability"] == "payable"
        return cls(payable)

    def __init__(self, payable: bool = False):
        self.payable = payable

    def __str__(self) -> str:
        return "receive() " + ("payable" if self.payable else "nonpayable")


class ConstructorCall:
    """A call to the contract's constructor."""

    input_bytes: bytes
    """Encoded call arguments."""

    def __init__(self, input_bytes: bytes):
        self.input_bytes = input_bytes


class MethodCall:
    """A call to a contract's regular method."""

    data_bytes: bytes
    """Encoded call arguments with the selector."""

    def __init__(self, data_bytes: bytes):
        self.data_bytes = data_bytes


# This is force-documented as :py:class in ``api.rst``
# because Sphinx cannot resolve typevars correctly.
# See https://github.com/sphinx-doc/sphinx/issues/9705
MethodType = TypeVar("MethodType")


class Methods(Generic[MethodType]):
    """
    Bases: ``Generic`` [``MethodType``]

    A holder for named methods which can be accessed as attributes,
    or iterated over.
    """

    # :show-inheritance: is turned off in ``api.rst``, and we are documenting the base manually
    # (although without hyperlinking which I cannot get to work).
    # See https://github.com/sphinx-doc/sphinx/issues/9705

    def __init__(self, methods_dict: Mapping[str, MethodType]):
        self._methods_dict = methods_dict

    def __getattr__(self, method_name: str) -> MethodType:
        """Returns the method by name."""
        return self._methods_dict[method_name]

    def __iter__(self) -> Iterator[MethodType]:
        """Returns the iterator over all methods."""
        return iter(self._methods_dict.values())


PANIC_ERROR = Error("Panic", dict(code=abi.uint(256)))


LEGACY_ERROR = Error("Error", dict(message=abi.string))


class UnknownError(Exception):
    pass


class ContractABI:
    """
    A wrapper for contract ABI.

    Contract methods are grouped by type and are accessible via the attributes below.
    """

    constructor: Constructor
    """Contract's constructor."""

    fallback: Optional[Fallback]
    """Contract's fallback method."""

    receive: Optional[Receive]
    """Contract's receive method."""

    method: Methods[Method]
    """Contract's regular methods."""

    event: Methods[Event]
    """Contract's events."""

    error: Methods[Error]
    """Contract's errors."""

    @classmethod
    def from_json(cls, json_abi: List[Dict[str, JSON]]) -> "ContractABI":  # noqa: C901
        """Creates this object from a JSON ABI (e.g. generated by a Solidity compiler)."""
        constructor = None
        fallback = None
        receive = None
        methods = {}
        events = {}
        errors = {}

        for entry in json_abi:
            if entry["type"] == "constructor":
                if constructor:
                    raise ValueError("JSON ABI contains more than one constructor declarations")
                constructor = Constructor.from_json(entry)

            elif entry["type"] == "function":
                if entry["name"] in methods:
                    # TODO: add support for overloaded methods
                    raise ValueError(
                        f"JSON ABI contains more than one declarations of `{entry['name']}`"
                    )
                methods[entry["name"]] = Method.from_json(entry)

            elif entry["type"] == "fallback":
                if fallback:
                    raise ValueError("JSON ABI contains more than one fallback declarations")
                fallback = Fallback.from_json(entry)

            elif entry["type"] == "receive":
                if receive:
                    raise ValueError("JSON ABI contains more than one receive method declarations")
                receive = Receive.from_json(entry)

            elif entry["type"] == "event":
                if entry["name"] in events:
                    raise ValueError(
                        f"JSON ABI contains more than one declarations of `{entry['name']}`"
                    )
                events[entry["name"]] = Event.from_json(entry)

            elif entry["type"] == "error":
                if entry["name"] in errors:
                    raise ValueError(
                        f"JSON ABI contains more than one declarations of `{entry['name']}`"
                    )
                errors[entry["name"]] = Error.from_json(entry)

            else:
                raise ValueError(f"Unknown ABI entry type: {entry['type']}")

        return cls(
            constructor=constructor,
            fallback=fallback,
            receive=receive,
            methods=methods.values(),
            events=events.values(),
            errors=errors.values(),
        )

    def __init__(
        self,
        constructor: Optional[Constructor] = None,
        fallback: Optional[Fallback] = None,
        receive: Optional[Receive] = None,
        methods: Optional[Iterable[Method]] = None,
        events: Optional[Iterable[Event]] = None,
        errors: Optional[Iterable[Error]] = None,
    ):
        if constructor is None:
            constructor = Constructor(inputs=[])

        self.fallback = fallback
        self.receive = receive
        self.constructor = constructor
        self.method = Methods({method.name: method for method in (methods or [])})
        self.event = Methods({event.name: event for event in (events or [])})
        self.error = Methods({error.name: error for error in (errors or [])})

        self._error_by_selector = {
            error.selector: error for error in chain([PANIC_ERROR, LEGACY_ERROR], self.error)
        }

    def resolve_error(self, error_data: bytes) -> Tuple[Error, Dict[str, Any]]:
        """
        Given the packed error data, attempts to find the error in the ABI
        and decode the data into its fields.
        """
        if len(error_data) < 4:
            raise ValueError("Error data too short to contain a selector")

        selector, data = error_data[:4], error_data[4:]

        if selector in self._error_by_selector:
            error = self._error_by_selector[selector]
            decoded = error.decode_fields(data)
            return error, decoded

        raise UnknownError(f"Could not find an error with selector {selector.hex()} in the ABI")

    def __str__(self) -> str:
        all_methods: Iterable[Union[Constructor, Fallback, Receive, Method, Event, Error]] = chain(
            [self.constructor] if self.constructor else [],
            [self.fallback] if self.fallback else [],
            [self.receive] if self.receive else [],
            self.method,
            self.event,
            self.error,
        )
        method_list = ["    " + str(method) for method in all_methods]
        return "{\n" + "\n".join(method_list) + "\n}"
