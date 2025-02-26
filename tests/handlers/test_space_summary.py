#  Copyright 2021 The Matrix.org Foundation C.I.C.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
from typing import Any, Iterable, Optional, Tuple
from unittest import mock

from synapse.api.constants import EventContentFields, JoinRules, RoomTypes
from synapse.api.errors import AuthError
from synapse.handlers.space_summary import _child_events_comparison_key
from synapse.rest import admin
from synapse.rest.client.v1 import login, room
from synapse.server import HomeServer
from synapse.types import JsonDict

from tests import unittest


def _create_event(room_id: str, order: Optional[Any] = None):
    result = mock.Mock()
    result.room_id = room_id
    result.content = {}
    if order is not None:
        result.content["order"] = order
    return result


def _order(*events):
    return sorted(events, key=_child_events_comparison_key)


class TestSpaceSummarySort(unittest.TestCase):
    def test_no_order_last(self):
        """An event with no ordering is placed behind those with an ordering."""
        ev1 = _create_event("!abc:test")
        ev2 = _create_event("!xyz:test", "xyz")

        self.assertEqual([ev2, ev1], _order(ev1, ev2))

    def test_order(self):
        """The ordering should be used."""
        ev1 = _create_event("!abc:test", "xyz")
        ev2 = _create_event("!xyz:test", "abc")

        self.assertEqual([ev2, ev1], _order(ev1, ev2))

    def test_order_room_id(self):
        """Room ID is a tie-breaker for ordering."""
        ev1 = _create_event("!abc:test", "abc")
        ev2 = _create_event("!xyz:test", "abc")

        self.assertEqual([ev1, ev2], _order(ev1, ev2))

    def test_invalid_ordering_type(self):
        """Invalid orderings are considered the same as missing."""
        ev1 = _create_event("!abc:test", 1)
        ev2 = _create_event("!xyz:test", "xyz")

        self.assertEqual([ev2, ev1], _order(ev1, ev2))

        ev1 = _create_event("!abc:test", {})
        self.assertEqual([ev2, ev1], _order(ev1, ev2))

        ev1 = _create_event("!abc:test", [])
        self.assertEqual([ev2, ev1], _order(ev1, ev2))

        ev1 = _create_event("!abc:test", True)
        self.assertEqual([ev2, ev1], _order(ev1, ev2))

    def test_invalid_ordering_value(self):
        """Invalid orderings are considered the same as missing."""
        ev1 = _create_event("!abc:test", "foo\n")
        ev2 = _create_event("!xyz:test", "xyz")

        self.assertEqual([ev2, ev1], _order(ev1, ev2))

        ev1 = _create_event("!abc:test", "a" * 51)
        self.assertEqual([ev2, ev1], _order(ev1, ev2))


class SpaceSummaryTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor, clock, hs: HomeServer):
        self.hs = hs
        self.handler = self.hs.get_space_summary_handler()

        # Create a user.
        self.user = self.register_user("user", "pass")
        self.token = self.login("user", "pass")

        # Create a space and a child room.
        self.space = self.helper.create_room_as(
            self.user,
            tok=self.token,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        self.room = self.helper.create_room_as(self.user, tok=self.token)
        self._add_child(self.space, self.room, self.token)

    def _add_child(self, space_id: str, room_id: str, token: str) -> None:
        """Add a child room to a space."""
        self.helper.send_state(
            space_id,
            event_type="m.space.child",
            body={"via": [self.hs.hostname]},
            tok=token,
            state_key=room_id,
        )

    def _assert_rooms(self, result: JsonDict, rooms: Iterable[str]) -> None:
        """Assert that the expected room IDs are in the response."""
        self.assertCountEqual([room.get("room_id") for room in result["rooms"]], rooms)

    def _assert_events(
        self, result: JsonDict, events: Iterable[Tuple[str, str]]
    ) -> None:
        """Assert that the expected parent / child room IDs are in the response."""
        self.assertCountEqual(
            [
                (event.get("room_id"), event.get("state_key"))
                for event in result["events"]
            ],
            events,
        )

    def test_simple_space(self):
        """Test a simple space with a single room."""
        result = self.get_success(self.handler.get_space_summary(self.user, self.space))
        # The result should have the space and the room in it, along with a link
        # from space -> room.
        self._assert_rooms(result, [self.space, self.room])
        self._assert_events(result, [(self.space, self.room)])

    def test_visibility(self):
        """A user not in a space cannot inspect it."""
        user2 = self.register_user("user2", "pass")
        token2 = self.login("user2", "pass")

        # The user cannot see the space.
        self.get_failure(self.handler.get_space_summary(user2, self.space), AuthError)

        # Joining the room causes it to be visible.
        self.helper.join(self.space, user2, tok=token2)
        result = self.get_success(self.handler.get_space_summary(user2, self.space))

        # The result should only have the space, but includes the link to the room.
        self._assert_rooms(result, [self.space])
        self._assert_events(result, [(self.space, self.room)])

    def test_world_readable(self):
        """A world-readable room is visible to everyone."""
        self.helper.send_state(
            self.space,
            event_type="m.room.history_visibility",
            body={"history_visibility": "world_readable"},
            tok=self.token,
        )

        user2 = self.register_user("user2", "pass")

        # The space should be visible, as well as the link to the room.
        result = self.get_success(self.handler.get_space_summary(user2, self.space))
        self._assert_rooms(result, [self.space])
        self._assert_events(result, [(self.space, self.room)])

    def test_complex_space(self):
        """
        Create a "complex" space to see how it handles things like loops and subspaces.
        """
        # Create an inaccessible room.
        user2 = self.register_user("user2", "pass")
        token2 = self.login("user2", "pass")
        room2 = self.helper.create_room_as(user2, tok=token2)
        # This is a bit odd as "user" is adding a room they don't know about, but
        # it works for the tests.
        self._add_child(self.space, room2, self.token)

        # Create a subspace under the space with an additional room in it.
        subspace = self.helper.create_room_as(
            self.user,
            tok=self.token,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        subroom = self.helper.create_room_as(self.user, tok=self.token)
        self._add_child(self.space, subspace, token=self.token)
        self._add_child(subspace, subroom, token=self.token)
        # Also add the two rooms from the space into this subspace (causing loops).
        self._add_child(subspace, self.room, token=self.token)
        self._add_child(subspace, room2, self.token)

        result = self.get_success(self.handler.get_space_summary(self.user, self.space))

        # The result should include each room a single time and each link.
        self._assert_rooms(result, [self.space, self.room, subspace, subroom])
        self._assert_events(
            result,
            [
                (self.space, self.room),
                (self.space, room2),
                (self.space, subspace),
                (subspace, subroom),
                (subspace, self.room),
                (subspace, room2),
            ],
        )

    def test_fed_complex(self):
        """
        Return data over federation and ensure that it is handled properly.
        """
        fed_hostname = self.hs.hostname + "2"
        subspace = "#subspace:" + fed_hostname
        subroom = "#subroom:" + fed_hostname

        async def summarize_remote_room(
            _self, room, suggested_only, max_children, exclude_rooms
        ):
            # Return some good data, and some bad data:
            #
            # * Event *back* to the root room.
            # * Unrelated events / rooms
            # * Multiple levels of events (in a not-useful order, e.g. grandchild
            #   events before child events).

            # Note that these entries are brief, but should contain enough info.
            rooms = [
                {
                    "room_id": subspace,
                    "world_readable": True,
                    "room_type": RoomTypes.SPACE,
                },
                {
                    "room_id": subroom,
                    "world_readable": True,
                },
            ]
            event_content = {"via": [fed_hostname]}
            events = [
                {
                    "room_id": subspace,
                    "state_key": subroom,
                    "content": event_content,
                },
            ]
            return rooms, events

        # Add a room to the space which is on another server.
        self._add_child(self.space, subspace, self.token)

        with mock.patch(
            "synapse.handlers.space_summary.SpaceSummaryHandler._summarize_remote_room",
            new=summarize_remote_room,
        ):
            result = self.get_success(
                self.handler.get_space_summary(self.user, self.space)
            )

        self._assert_rooms(result, [self.space, self.room, subspace, subroom])
        self._assert_events(
            result,
            [
                (self.space, self.room),
                (self.space, subspace),
                (subspace, subroom),
            ],
        )

    def test_fed_filtering(self):
        """
        Rooms returned over federation should be properly filtered to only include
        rooms the user has access to.
        """
        fed_hostname = self.hs.hostname + "2"
        subspace = "#subspace:" + fed_hostname

        # Create a few rooms which will have different properties.
        restricted_room = "#restricted:" + fed_hostname
        restricted_accessible_room = "#restricted_accessible:" + fed_hostname
        world_readable_room = "#world_readable:" + fed_hostname
        joined_room = self.helper.create_room_as(self.user, tok=self.token)

        async def summarize_remote_room(
            _self, room, suggested_only, max_children, exclude_rooms
        ):
            # Note that these entries are brief, but should contain enough info.
            rooms = [
                {
                    "room_id": restricted_room,
                    "world_readable": False,
                    "join_rules": JoinRules.MSC3083_RESTRICTED,
                    "allowed_spaces": [],
                },
                {
                    "room_id": restricted_accessible_room,
                    "world_readable": False,
                    "join_rules": JoinRules.MSC3083_RESTRICTED,
                    "allowed_spaces": [self.room],
                },
                {
                    "room_id": world_readable_room,
                    "world_readable": True,
                    "join_rules": JoinRules.INVITE,
                },
                {
                    "room_id": joined_room,
                    "world_readable": False,
                    "join_rules": JoinRules.INVITE,
                },
            ]

            # Place each room in the sub-space.
            event_content = {"via": [fed_hostname]}
            events = [
                {
                    "room_id": subspace,
                    "state_key": room["room_id"],
                    "content": event_content,
                }
                for room in rooms
            ]

            # Also include the subspace.
            rooms.insert(
                0,
                {
                    "room_id": subspace,
                    "world_readable": True,
                },
            )
            return rooms, events

        # Add a room to the space which is on another server.
        self._add_child(self.space, subspace, self.token)

        with mock.patch(
            "synapse.handlers.space_summary.SpaceSummaryHandler._summarize_remote_room",
            new=summarize_remote_room,
        ):
            result = self.get_success(
                self.handler.get_space_summary(self.user, self.space)
            )

        self._assert_rooms(
            result,
            [
                self.space,
                self.room,
                subspace,
                restricted_accessible_room,
                world_readable_room,
                joined_room,
            ],
        )
        self._assert_events(
            result,
            [
                (self.space, self.room),
                (self.space, subspace),
                (subspace, restricted_room),
                (subspace, restricted_accessible_room),
                (subspace, world_readable_room),
                (subspace, joined_room),
            ],
        )
