"""Classes that represent the state of the entire system and entities within.

These classes wrap protobufs, which are basically a fancy NamedTuple that is
generated by the `build` Makefile target. You can read more about protobufs
online, but mainly they're helpful for serializing data over the network."""

import logging
from enum import Enum
from typing import List, Dict, Optional, Union

import numpy as np
import vpython

from orbitx import orbitx_pb2 as protos
from orbitx import strings

log = logging.getLogger()

# This Request class is just an alias of the Command protobuf message. We
# provide this so that nobody has to directly import orbitx_pb2, and so that
# we can this wrapper class in the future.
Request = protos.Command

# These entity fields do not change during simulation. Thus, we don't have to
# store them in a big 1D numpy array for use in scipy.solve_ivp.
_PER_ENTITY_UNCHANGING_FIELDS = [
    'name', 'mass', 'r', 'artificial', 'atmosphere_thickness',
    'atmosphere_scaling'
]

_PER_ENTITY_MUTABLE_FIELDS = [field.name for
                              field in protos.Entity.DESCRIPTOR.fields if
                              field.name not in _PER_ENTITY_UNCHANGING_FIELDS]
_ENTITY_FIELD_ORDER = {name: index for index, name in
                       enumerate(_PER_ENTITY_MUTABLE_FIELDS)}

_N_COMPONENTS = len(strings.COMPONENT_NAMES)
_N_COOLANT_LOOPS = len(strings.COOLANT_LOOP_NAMES)
_N_RADIATORS = len(strings.RADIATOR_NAMES)
_N_COMPONENT_FIELDS = len(protos.EngineeringState.Component.DESCRIPTOR.fields)
_N_COOLANT_FIELDS = len(protos.EngineeringState.CoolantLoop.DESCRIPTOR.fields)
_N_RADIATOR_FIELDS = len(protos.EngineeringState.Radiator.DESCRIPTOR.fields)

# A special field, we reference it a couple times so turn it into a symbol
# to guard against string literal typos.
_LANDED_ON = "landed_on"
assert _LANDED_ON in [field.name for field in protos.Entity.DESCRIPTOR.fields]

# Make sure this is in sync with the corresponding enum in orbitx.proto!
Navmode = Enum('Navmode', zip([  # type: ignore
    'Manual', 'CCW Prograde', 'CW Retrograde', 'Depart Reference',
    'Approach Target', 'Pro Targ Velocity', 'Anti Targ Velocity'
], protos.Navmode.values()))


class Entity:
    """A wrapper around protos.Entity.

    Example usage:
    assert Entity(protos.Entity(x=5)).x == 5
    assert Entity(protos.Entity(x=1, y=2)).pos == [1, 2]

    To add fields, or see what fields exists, please see orbitx.proto,
    specifically the "message Entity" declaration.
    """

    def __init__(self, entity: protos.Entity):
        self.proto = entity

    def __repr__(self):
        return self.proto.__repr__()

    def __str__(self):
        return self.proto.__str__()

    # These are filled in just below this class definition. These stubs are for
    # static type analysis with mypy.
    name: str
    x: float
    y: float
    vx: float
    vy: float
    r: float
    mass: float
    heading: float
    spin: float
    fuel: float
    throttle: float
    landed_on: str
    broken: bool
    artificial: bool
    atmosphere_thickness: float
    atmosphere_scaling: float

    def screen_pos(self, origin: 'Entity') -> vpython.vector:
        """The on-screen position of this entity, relative to the origin."""
        return vpython.vector(self.x - origin.x, self.y - origin.y, 0)

    @property
    def pos(self):
        return np.array((self.x, self.y), dtype=PhysicsState.DTYPE, copy=True)

    @pos.setter
    def pos(self, coord):
        self.x = coord[0]
        self.y = coord[1]

    @property
    def v(self):
        return np.asarray([self.vx, self.vy])

    @v.setter
    def v(self, coord):
        self.vx = coord[0]
        self.vy = coord[1]

    @property
    def dockable(self):
        return self.name == strings.AYSE

    def landed(self) -> bool:
        """Convenient and more elegant check to see if the entity is landed."""
        return self.landed_on != ''


class _EntityView(Entity):
    """A view into a PhysicsState, very fast to create and use.
    Setting fields will update the parent PhysicsState appropriately."""

    def __init__(self, creator: 'PhysicsState', index: int):
        self._creator = creator
        self._index = index

    def __repr__(self):
        # This is actually a bit hacky. This line implies that orbitx_pb2
        # protobuf generated code can't tell the difference between an
        # orbitx_pb2.Entity and an _EntityView. Turns out, it can't! But
        # hopefully this assumption always holds.
        return repr(Entity(self))

    def __str__(self):
        return str(Entity(self))


# I feel like I should apologize before things get too crazy. Once you read
# the following module-level loop and ask "why _EntityView a janky subclass of
# Entity, and is implemented using janky array indexing into data owned by a
# PhysicsState?".
# My excuse is that I wanted a way to index into PhysicsState and get an Entity
# for ease of use and code. I found this to be a useful API that made physics
# code cleaner, but it was _too_ useful! The PhysicsState.__getitem__ method
# that implemented this indexing was so expensive and called so often that it
# was _half_ the runtime of OrbitX at high time accelerations! My solution to
# this performance issue was to optimize PhysicsState.__getitem__ by return
# an Entity (specifically, an _EntityView) that was very fast to instantiate
# and very fast to access.
# Hence: janky array-indexing accessors is my super-optimization! 2x speedup!
for field in protos.Entity.DESCRIPTOR.fields:
    # For every field in the underlying protobuf entity, make a
    # convenient equivalent property to allow code like the following:
    # Entity(entity).heading = 5

    def entity_fget(self, name=field.name):
        return getattr(self.proto, name)


    def entity_fset(self, val, name=field.name):
        return setattr(self.proto, name, val)


    def entity_fdel(self, name=field.name):
        return delattr(self.proto, name)


    setattr(Entity, field.name, property(
        fget=entity_fget, fset=entity_fset, fdel=entity_fdel,
        doc=f"Entity proxy of the underlying field, self.proto.{field.name}"))


    def entity_view_unchanging_fget(self, name=field.name):
        return getattr(self._creator._proto_state.entities[self._index], name)


    def entity_view_unchanging_fset(self, val, name=field.name):
        return setattr(
            self._creator._proto_state.entities[self._index], name, val)


    field_n: Optional[int]
    if field.name in _PER_ENTITY_MUTABLE_FIELDS:
        field_n = _ENTITY_FIELD_ORDER[field.name]
    else:
        field_n = None

    if field.cpp_type in [field.CPPTYPE_FLOAT, field.CPPTYPE_DOUBLE]:
        def entity_view_mutable_fget(self, field_n=field_n):
            return self._creator._array_rep[
                self._creator._n * field_n + self._index]


        def entity_view_mutable_fset(self, val, field_n=field_n):
            self._creator._array_rep[
                self._creator._n * field_n + self._index] = val
    elif field.cpp_type == field.CPPTYPE_BOOL:
        # Same as if it's a float, but we have to convert float -> bool.
        def entity_view_mutable_fget(self, field_n=field_n):
            return bool(
                self._creator._array_rep[
                    self._creator._n * field_n + self._index])


        def entity_view_mutable_fset(self, val, field_n=field_n):
            self._creator._array_rep[
                self._creator._n * field_n + self._index] = val
    elif field.name == _LANDED_ON:
        # Special case, we store the index of the entity we're landed on as a
        # float, but we have to convert this to an int then the name of the
        # entity.
        def entity_view_mutable_fget(self, field_n=field_n):
            entity_index = int(
                self._creator._array_rep[
                    self._creator._n * field_n + self._index])
            if entity_index == PhysicsState.NO_INDEX:
                return ''
            return self._creator._entity_names[entity_index]


        def entity_view_mutable_fset(self, val, field_n=field_n):
            assert isinstance(val, str)
            self._creator._array_rep[
                self._creator._n * field_n + self._index] = \
                self._creator._name_to_index(val)
    elif field.cpp_type == field.CPPTYPE_STRING:
        assert field.name in _PER_ENTITY_UNCHANGING_FIELDS
    else:
        raise NotImplementedError(
            "Encountered a field in the protobuf definition of Entity that "
            "is of a type we haven't handled.")

    if field.name in _PER_ENTITY_UNCHANGING_FIELDS:
        # Note there is no fdel defined. The data is owned by the PhysicalState
        # so the PhysicalState should delete data on its own time.
        setattr(_EntityView, field.name, property(
            fget=entity_view_unchanging_fget,
            fset=entity_view_unchanging_fset,
            doc=f"_EntityView proxy of unchanging field {field.name}"
        ))

    else:
        assert field.name in _PER_ENTITY_MUTABLE_FIELDS
        setattr(_EntityView, field.name, property(
            fget=entity_view_mutable_fget,
            fset=entity_view_mutable_fset,
            doc=f"_EntityView proxy of mutable field {field.name}"
        ))


class CoolantView:
    """Represents a single Coolant Loop.

    Should not be instantiated outside of EngineeringState."""

    def __init__(self, array_rep: np.ndarray, coolant_n: int):
        """Called by an EngineeringState factory.

        array_rep: an array that, starting at 0, contains all data for all components.
        coolant_n: an index specifying which coolant loop, starting at 0.
        """
        self._array = array_rep
        self._n = coolant_n

    def name(self):
        return strings.COOLANT_LOOP_NAMES[self._n]

    @property
    def coolant_temp(self) -> float:
        return self._array[self._n * _N_COOLANT_FIELDS + 0]

    @coolant_temp.setter
    def coolant_temp(self, val: float):
        self._array[self._n * _N_COOLANT_FIELDS + 0] = val

    @property
    def primary_pump_on(self) -> bool:
        return bool(self._array[self._n * _N_COOLANT_FIELDS + 1])

    @primary_pump_on.setter
    def primary_pump_on(self, val: bool):
        self._array[self._n * _N_COOLANT_FIELDS + 1] = val

    @property
    def secondary_pump_on(self) -> bool:
        return bool(self._array[self._n * _N_COOLANT_FIELDS + 2])

    @secondary_pump_on.setter
    def secondary_pump_on(self, val: bool):
        self._array[self._n * _N_COOLANT_FIELDS + 2] = val


class ComponentView:
    """Represents a single Component.

    Should not be instantiated outside of EngineeringState."""

    def __init__(self, parent: 'EngineeringState', array_rep: np.ndarray, component_n: int):
        """Called by an EngineeringState factory.

        array_rep: an array that, starting at 0, contains all data for all components.
        component_n: an index specifying which component, starting at 0.
        """
        self._parent = parent
        self._array = array_rep
        self._n = component_n

    def name(self):
        return strings.COMPONENT_NAMES[self._n]

    @property
    def connected(self) -> bool:
        return bool(self._array[self._n * _N_COMPONENT_FIELDS + 0])

    @connected.setter
    def connected(self, val: bool):
        self._array[self._n * _N_COMPONENT_FIELDS + 0] = val

    @property
    def temperature(self) -> float:
        return self._array[self._n * _N_COMPONENT_FIELDS + 1]

    @temperature.setter
    def temperature(self, val: float):
        self._array[self._n * _N_COMPONENT_FIELDS + 1] = val

    @property
    def resistance(self) -> float:
        return self._array[self._n * _N_COMPONENT_FIELDS + 2]

    @resistance.setter
    def resistance(self, val: float):
        self._array[self._n * _N_COMPONENT_FIELDS + 2] = val

    @property
    def voltage(self) -> float:
        return self._array[self._n * _N_COMPONENT_FIELDS + 3]

    @voltage.setter
    def voltage(self, val: float):
        self._array[self._n * _N_COMPONENT_FIELDS + 3] = val

    @property
    def current(self) -> float:
        return self._array[self._n * _N_COMPONENT_FIELDS + 4]

    @current.setter
    def current(self, val: float):
        self._array[self._n * _N_COMPONENT_FIELDS + 4] = val

    def get_coolant_loop(self) -> CoolantView:
        return self._parent.coolant_loops[self.attached_to_coolant_loop - 1]

    @property
    def attached_to_coolant_loop(self) -> int:
        return int(self._array[self._n * _N_COMPONENT_FIELDS + 5])

    @attached_to_coolant_loop.setter
    def attached_to_coolant_loop(self, val: int):
        self._array[self._n * _N_COMPONENT_FIELDS + 5] = val


class RadiatorView:
    """Represents a single Radiator.

    Should not be instantiated outside of EngineeringState.

    Useful function: get_coolant_loop()! Gives the coolant loop this radiator is attached to.
    e.g.

    physics_state.engineering.radiator[RAD2].get_coolant_loop().coolant_temp
    """

    def __init__(self, parent: 'EngineeringState', array_rep: np.ndarray, radiator_n: int):
        """Called by an EngineeringState factory.

        parent: an EngineeringState that this RadiatorView will use to get the associated coolant loop.
        array_rep: an array that, starting at 0, contains all data for all radiators.
        radiator_n: an index specifying which component, starting at 0.
        """
        self._parent = parent
        self._array = array_rep
        self._n = radiator_n

    def name(self):
        return strings.RADIATOR_NAMES[self._n]

    def get_coolant_loop(self) -> CoolantView:
        return self._parent.coolant_loops[self.attached_to_coolant_loop - 1]

    @property
    def attached_to_coolant_loop(self) -> int:
        return int(self._array[self._n * _N_RADIATOR_FIELDS + 0])

    @attached_to_coolant_loop.setter
    def attached_to_coolant_loop(self, val: int):
        self._array[self._n * _N_RADIATOR_FIELDS + 0] = val

    @property
    def functioning(self) -> bool:
        return bool(self._array[self._n * _N_RADIATOR_FIELDS + 1])

    @functioning.setter
    def functioning(self, val: bool):
        self._array[self._n * _N_RADIATOR_FIELDS + 1] = val


class EngineeringState:
    """Wrapper around protos.EngineeringState.

    Access with physics_state.engineering, e.g.
        eng_state = physics_state.engineering
        eng_state.master_alarm = True
        print(eng_state.components[AUXCOM].resistance)
        eng_state.components[LOS].connected = True
        eng_state.radiators[RAD2].functioning = False
        eng_state.radiators[RAD2].get_coolant_loop().coolant_temp = 50
    """

    N_ENGINEERING_FIELDS = (
        _N_COMPONENTS * _N_COMPONENT_FIELDS +
        _N_COOLANT_LOOPS * _N_COOLANT_FIELDS +
        _N_RADIATORS * _N_RADIATOR_FIELDS
    )

    _COMPONENT_START_INDEX = 0
    _COOLANT_START_INDEX = _N_COMPONENTS * _N_COMPONENT_FIELDS
    _RADIATOR_START_INDEX = _COOLANT_START_INDEX + _N_COOLANT_LOOPS * _N_COOLANT_FIELDS

    class ComponentList:
        """Allows engineering.components[LOS] style indexing."""
        def __init__(self, owner: 'EngineeringState'):
            self._owner = owner

        def __getitem__(self, index: Union[str, int]) -> ComponentView:
            if isinstance(index, str):
                index = strings.COMPONENT_NAMES.index(index)
            elif index >= _N_COMPONENTS:
                raise IndexError()
            return ComponentView(
                self._owner,
                self._owner._array[self._owner._COMPONENT_START_INDEX:self._owner._COOLANT_START_INDEX],
                index
            )

    class CoolantLoopList:
        """Allows engineering.coolant_loops[LP1] style indexing."""
        def __init__(self, owner: 'EngineeringState'):
            self._owner = owner

        def __getitem__(self, index: Union[str, int]) -> CoolantView:
            if isinstance(index, str):
                index = strings.COOLANT_LOOP_NAMES.index(index)
            elif index >= _N_COOLANT_LOOPS:
                raise IndexError()
            return CoolantView(
                self._owner._array[self._owner._COOLANT_START_INDEX:self._owner._RADIATOR_START_INDEX],
                index
            )

    class RadiatorList:
        """Allows engineering.radiators[RAD1] style indexing."""
        def __init__(self, owner: 'EngineeringState'):
            self._owner = owner

        def __getitem__(self, index: Union[str, int]) -> RadiatorView:
            if isinstance(index, str):
                index = strings.RADIATOR_NAMES.index(index)
            elif index >= _N_RADIATORS:
                raise IndexError()
            return RadiatorView(
                self._owner,
                self._owner._array[self._owner._RADIATOR_START_INDEX:],
                index
            )

    def __init__(self, array_rep: np.ndarray, proto_state: protos.EngineeringState, populate_array: bool):
        """Called by a PhysicsState on creation.

        array_rep: a sufficiently-sized array to store all component, coolant,
                   and radiator data. EngineeringState has full control over
                   contents, starting at element 0.
        proto_state: the underlying proto we're wrapping.
        populate_array: flag that is set when we need to fill array_rep with data.
        """
        assert len(proto_state.components) == _N_COMPONENTS
        assert len(proto_state.coolant_loops) == _N_COOLANT_LOOPS
        assert len(proto_state.radiators) == _N_RADIATORS

        self.components = self.ComponentList(self)
        self.coolant_loops = self.CoolantLoopList(self)
        self.radiators = self.RadiatorList(self)

        self._array = array_rep
        self._proto_state = proto_state

        if populate_array:
            # We've been asked to populate the data array.
            # The order of data in the array is of course important.
            write_marker = 0

            # Is this loop janky? I would say yes! Could this result in
            # out-of-bounds writes? I hope not!
            for proto_list, descriptor in [
                (proto_state.components, protos.EngineeringState.Component.DESCRIPTOR),
                (proto_state.coolant_loops, protos.EngineeringState.CoolantLoop.DESCRIPTOR),
                (proto_state.radiators, protos.EngineeringState.Radiator.DESCRIPTOR),
            ]:
                for proto in proto_list:
                    for field in descriptor.fields:
                        array_rep[write_marker] = getattr(proto, field.name)
                        write_marker += 1

    @property
    def master_alarm(self) -> bool:
        return self._proto_state.master_alarm

    @master_alarm.setter
    def master_alarm(self, val: bool):
        self._proto_state.master_alarm = val

    @property
    def radiation_alarm(self) -> bool:
        return self._proto_state.radiation_alarm

    @radiation_alarm.setter
    def radiation_alarm(self, val: bool):
        self._proto_state.radiation_alarm = val

    @property
    def asteroid_alarm(self) -> bool:
        return self._proto_state.asteroid_alarm

    @asteroid_alarm.setter
    def asteroid_alarm(self, val: bool):
        self._proto_state.asteroid_alarm = val

    @property
    def hab_reactor_alarm(self) -> bool:
        return self._proto_state.hab_reactor_alarm

    @hab_reactor_alarm.setter
    def hab_reactor_alarm(self, val: bool):
        self._proto_state.hab_reactor_alarm = val

    @property
    def ayse_reactor_alarm(self) -> bool:
        return self._proto_state.ayse_reactor_alarm

    @ayse_reactor_alarm.setter
    def ayse_reactor_alarm(self, val: bool):
        self._proto_state.ayse_reactor_alarm = val

    @property
    def hab_gnomes(self) -> bool:
        return self._proto_state.hab_gnomes

    @hab_gnomes.setter
    def hab_gnomes(self, val: bool):
        self._proto_state.hab_gnomes = val

    def as_proto(self) -> protos.EngineeringState:
        """Returns a deep copy of this EngineeringState as a protobuf."""
        constructed_protobuf = protos.EngineeringState()
        constructed_protobuf.CopyFrom(self._proto_state)
        for component_data, component in zip(self.components, constructed_protobuf.components):
            (
                component.connected, component.temperature,
                component.resistance, component.voltage,
                component.current
            ) = (
                component_data.connected, component_data.temperature,
                component_data.resistance, component_data.voltage,
                component_data.current
            )
        for coolant_data, coolant in zip(self.coolant_loops, constructed_protobuf.coolant_loops):
            (
                coolant.coolant_temp, coolant.primary_pump_on,
                coolant.secondary_pump_on
            ) = (
                coolant_data.coolant_temp, coolant_data.primary_pump_on,
                coolant_data.secondary_pump_on
            )
        for radiator_data, radiator in zip(self.radiators, constructed_protobuf.radiators):
            (
                radiator.attached_to_coolant_loop, radiator.functioning,
            ) = (
                radiator_data.attached_to_coolant_loop, radiator_data.functioning,
            )
        return constructed_protobuf


class PhysicsState:
    """The physical state of the system for use in solve_ivp and elsewhere.

    The following operations are supported:

    # Construction without a y-vector, taking all data from a PhysicalState
    PhysicsState(None, protos.PhysicalState)

    # Faster Construction from a y-vector and protos.PhysicalState
    PhysicsState(ivp_solution.y, protos.PhysicalState)

    # Access of a single Entity in the PhysicsState, by index or Entity name
    my_entity: Entity = PhysicsState[0]
    my_entity: Entity = PhysicsState['Earth']

    # Iteration over all Entitys in the PhysicsState
    for entity in my_physics_state:
        print(entity.name, entity.pos)

    # Convert back to a protos.PhysicalState (this almost never happens)
    my_physics_state.as_proto()

    Example usage:
    y = PhysicsState(y_1d, physical_state)

    entity = y[0]
    y[HABITAT] = habitat
    scipy.solve_ivp(y.y0())

    See help(PhysicsState.__init__) for how to initialize. Basically, the `y`
    param should be None at the very start of the program, but for the program
    to have good performance, PhysicsState.__init__ should have both parameters
    filled if it's being called more than once a second while OrbitX is running
    normally.
    """

    class NoEntityError(ValueError):
        """Raised when an entity is not found."""
        pass

    # For if an entity is not landed to anything
    NO_INDEX = -1

    # The number of single-element values at the end of the y-vector.
    # Currently just SRB_TIME and TIME_ACC are appended to the end. If there
    # are more values appended to the end, increment this and follow the same
    # code for .srb_time and .time_acc
    N_SINGULAR_ELEMENTS = 2

    ENTITY_START_INDEX = 0
    ENGINEERING_START_INDEX = -(EngineeringState.N_ENGINEERING_FIELDS)
    SRB_TIME_INDEX = ENGINEERING_START_INDEX - 2
    TIME_ACC_INDEX = SRB_TIME_INDEX + 1

    # Datatype of internal y-vector
    DTYPE = np.float64

    def __init__(self,
                 y: Optional[np.ndarray],
                 proto_state: protos.PhysicalState):
        """Collects data from proto_state and y, when y is not None.

        There are two kinds of values we care about:
        1) values that change during simulation (like position, velocity, etc)
        2) values that do not change (like mass, radius, name, etc)

        If both proto_state and y are given, 1) is taken from y and
        2) is taken from proto_state. This is a very quick operation.

        If y is None, both 1) and 2) are taken from proto_state, and a new
        y vector is generated. This is a somewhat expensive operation."""
        assert isinstance(proto_state, protos.PhysicalState)
        assert isinstance(y, np.ndarray) or y is None

        # self._proto_state will have positions, velocities, etc for all
        # entities. DO NOT USE THESE they will be stale. Use the accessors of
        # this class instead!
        self._proto_state = protos.PhysicalState()
        self._proto_state.CopyFrom(proto_state)
        self._n = len(proto_state.entities)

        self._entity_names = \
            [entity.name for entity in self._proto_state.entities]
        self._array_rep: np.ndarray

        if y is None:
            # We rely on having an internal array representation we can refer
            # to, so we have to build up this array representation.
            self._array_rep = np.empty(
                len(proto_state.entities) * len(_PER_ENTITY_MUTABLE_FIELDS)
                + self.N_SINGULAR_ELEMENTS
                + EngineeringState.N_ENGINEERING_FIELDS, dtype=self.DTYPE)

            for field_name, field_n in _ENTITY_FIELD_ORDER.items():
                for entity_index, entity in enumerate(proto_state.entities):
                    proto_value = getattr(entity, field_name)
                    # Internally translate string names to indices, otherwise
                    # our entire y vector will turn into a string vector oh no.
                    # Note this will convert to floats, not integer indices.
                    if field_name == _LANDED_ON:
                        proto_value = self._name_to_index(proto_value)

                    self._array_rep[self._n * field_n + entity_index] = proto_value

            self._array_rep[self.SRB_TIME_INDEX] = proto_state.srb_time
            self._array_rep[self.TIME_ACC_INDEX] = proto_state.time_acc

            # It's IMPORTANT that we pass in self._array_rep, because otherwise the numpy
            # array will be copied and EngineeringState won't be modifying our numpy array.
            self.engineering = EngineeringState(
                self._array_rep[self.ENGINEERING_START_INDEX:],
                self._proto_state.engineering,
                populate_array=True
            )
        else:
            self._array_rep = y.astype(self.DTYPE)
            self._proto_state.srb_time = y[self.SRB_TIME_INDEX]
            self._proto_state.time_acc = y[self.TIME_ACC_INDEX]
            self.engineering = EngineeringState(self._array_rep[self.ENGINEERING_START_INDEX:], self._proto_state.engineering, populate_array=False )

        assert len(self._array_rep.shape) == 1, \
            f'y is not 1D: {self._array_rep.shape}'
        n_entities = len(proto_state.entities)
        assert self._array_rep.size == (
            n_entities * len(_PER_ENTITY_MUTABLE_FIELDS)
            + self.N_SINGULAR_ELEMENTS
            + EngineeringState.N_ENGINEERING_FIELDS
        )

        np.mod(self.Heading, 2 * np.pi, out=self.Heading)

        self._entities_with_atmospheres: Optional[List[int]] = None

    def _y_component(self, field_name: str) -> np.ndarray:
        """Returns an n-array with the value of a component for each entity."""
        return self._array_rep[
               _ENTITY_FIELD_ORDER[field_name] * self._n:
               (_ENTITY_FIELD_ORDER[field_name] + 1) * self._n
               ]

    def _index_to_name(self, index: int) -> str:
        """Translates an index into the entity list to the right name."""
        i = int(index)
        return self._entity_names[i] if i != self.NO_INDEX else ''

    def _name_to_index(self, name: Optional[str]) -> int:
        """Finds the index of the entity with the given name."""
        try:
            assert name is not None
            return self._entity_names.index(name) if name != '' \
                else self.NO_INDEX
        except ValueError:
            raise self.NoEntityError(f'{name} not in entity list')

    def y0(self):
        """Returns a y-vector suitable as input for scipy.solve_ivp."""
        return self._array_rep

    def as_proto(self) -> protos.PhysicalState:
        """Creates a protos.PhysicalState view into all internal data.

        Expensive. Consider one of the other accessors, which are faster.
        For example, if you want to iterate over all elements, use __iter__
        by doing:
        for entity in my_physics_state: print(entity.name)"""
        constructed_protobuf = protos.PhysicalState()
        constructed_protobuf.CopyFrom(self._proto_state)
        for entity_data, entity in zip(self, constructed_protobuf.entities):
            (
                entity.x, entity.y, entity.vx, entity.vy,
                entity.heading, entity.spin, entity.fuel,
                entity.throttle, entity.landed_on,
                entity.broken
            ) = (
                entity_data.x, entity_data.y, entity_data.vx, entity_data.vy,
                entity_data.heading, entity_data.spin, entity_data.fuel,
                entity_data.throttle, entity_data.landed_on,
                entity_data.broken
            )

        constructed_protobuf.engineering.CopyFrom(self.engineering.as_proto())

        return constructed_protobuf

    def __len__(self):
        """Implements `len(physics_state)`."""
        return self._n

    def __iter__(self):
        """Implements `for entity in physics_state:` loops."""
        for i in range(0, self._n):
            yield self.__getitem__(i)

    def __getitem__(self, index: Union[str, int]) -> Entity:
        """Returns a Entity view at a given name or index.

        Allows the following:
        entity = physics_state[2]
        entity = physics_state[HABITAT]
        entity.x = 5  # Propagates to physics_state.
        """
        if isinstance(index, str):
            # Turn a name-based index into an integer
            index = self._entity_names.index(index)
        i = int(index)

        return _EntityView(self, i)

    def __setitem__(self, index: Union[str, int], val: Entity):
        """Puts a Entity at a given name or index in the state.

        Allows the following:
        PhysicsState[2] = physics_entity
        PhysicsState[HABITAT] = physics_entity
        """
        if isinstance(val, _EntityView) and val._creator == self:
            # The _EntityView is a view into our own data, so we already have
            # the data.
            return
        if isinstance(index, str):
            # Turn a name-based index into an integer
            index = self._entity_names.index(index)
        i = int(index)

        entity = self[i]

        (
            entity.x, entity.y, entity.vx, entity.vy, entity.heading,
            entity.spin, entity.fuel, entity.throttle, entity.landed_on,
            entity.broken
        ) = (
            val.x, val.y, val.vx, val.vy, val.heading,
            val.spin, val.fuel, val.throttle, val.landed_on,
            val.broken
        )

    def __repr__(self):
        return self.as_proto().__repr__()

    def __str__(self):
        return self.as_proto().__str__()

    @property
    def timestamp(self) -> float:
        return self._proto_state.timestamp

    @timestamp.setter
    def timestamp(self, t: float):
        self._proto_state.timestamp = t

    @property
    def srb_time(self) -> float:
        return self._proto_state.srb_time

    @srb_time.setter
    def srb_time(self, val: float):
        self._proto_state.srb_time = val
        self._array_rep[self.SRB_TIME_INDEX] = val

    @property
    def parachute_deployed(self) -> bool:
        return self._proto_state.parachute_deployed

    @parachute_deployed.setter
    def parachute_deployed(self, val: bool):
        self._proto_state.parachute_deployed = val

    @property
    def X(self):
        return self._y_component('x')

    @property
    def Y(self):
        return self._y_component('y')

    @property
    def VX(self):
        return self._y_component('vx')

    @property
    def VY(self):
        return self._y_component('vy')

    @property
    def Heading(self):
        return self._y_component('heading')

    @property
    def Spin(self):
        return self._y_component('spin')

    @property
    def Fuel(self):
        return self._y_component('fuel')

    @property
    def Throttle(self):
        return self._y_component('throttle')

    @property
    def LandedOn(self) -> Dict[int, int]:
        """Returns a mapping from index to index of entity landings.

        If the 0th entity is landed on the 2nd entity, 0 -> 2 will be mapped.
        """
        landed_map = {}
        for landed, landee in enumerate(
                self._y_component('landed_on')):
            if int(landee) != self.NO_INDEX:
                landed_map[landed] = int(landee)
        return landed_map

    @property
    def Broken(self):
        return self._y_component('broken')

    @property
    def Atmospheres(self) -> List[int]:
        """Returns a list of indexes of entities that have an atmosphere."""
        if self._entities_with_atmospheres is None:
            self._entities_with_atmospheres = []
            for index, entity in enumerate(self._proto_state.entities):
                if entity.atmosphere_scaling != 0 and \
                        entity.atmosphere_thickness != 0:
                    self._entities_with_atmospheres.append(index)
        return self._entities_with_atmospheres

    @property
    def time_acc(self) -> float:
        """Returns the time acceleration, e.g. 1x or 50x."""
        return self._proto_state.time_acc

    @time_acc.setter
    def time_acc(self, new_acc: float):
        self._proto_state.time_acc = new_acc
        self._array_rep[self.TIME_ACC_INDEX] = new_acc

    def craft_entity(self):
        """Convenience function, a full Entity representing the craft."""
        return self[self.craft]

    @property
    def craft(self) -> Optional[str]:
        """Returns the currently-controlled craft.
        Not actually backed by any stored field, just a calculation."""
        if strings.HABITAT not in self._entity_names and \
                strings.AYSE not in self._entity_names:
            return None
        if strings.AYSE not in self._entity_names:
            return strings.HABITAT

        hab_index = self._name_to_index(strings.HABITAT)
        ayse_index = self._name_to_index(strings.AYSE)
        if self._y_component('landed_on')[hab_index] == ayse_index:
            # Habitat is docked with AYSE, AYSE is active craft
            return strings.AYSE
        else:
            return strings.HABITAT

    def reference_entity(self):
        """Convenience function, a full Entity representing the reference."""
        return self[self._proto_state.reference]

    @property
    def reference(self) -> str:
        """Returns current reference of the physics system, shown in GUI."""
        return self._proto_state.reference

    @reference.setter
    def reference(self, name: str):
        self._proto_state.reference = name

    def target_entity(self):
        """Convenience function, a full Entity representing the target."""
        return self[self._proto_state.target]

    @property
    def target(self) -> str:
        """Returns landing/docking target, shown in GUI."""
        return self._proto_state.target

    @target.setter
    def target(self, name: str):
        self._proto_state.target = name

    @property
    def navmode(self) -> Navmode:
        return Navmode(self._proto_state.navmode)

    @navmode.setter
    def navmode(self, navmode: Navmode):
        self._proto_state.navmode = navmode.value
