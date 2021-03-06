# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
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

import logging
from collections import namedtuple

from six import iteritems
from six.moves import range

from twisted.internet import defer

from synapse.api.constants import EventTypes
from synapse.storage._base import SQLBaseStore
from synapse.storage.data_stores.state.bg_updates import StateBackgroundUpdateStore
from synapse.storage.database import Database
from synapse.storage.state import StateFilter
from synapse.util.caches import get_cache_factor_for
from synapse.util.caches.descriptors import cached
from synapse.util.caches.dictionary_cache import DictionaryCache

logger = logging.getLogger(__name__)


MAX_STATE_DELTA_HOPS = 100


class _GetStateGroupDelta(
    namedtuple("_GetStateGroupDelta", ("prev_group", "delta_ids"))
):
    """Return type of get_state_group_delta that implements __len__, which lets
    us use the itrable flag when caching
    """

    __slots__ = []

    def __len__(self):
        return len(self.delta_ids) if self.delta_ids else 0


class StateGroupDataStore(StateBackgroundUpdateStore, SQLBaseStore):
    """A data store for fetching/storing state groups.
    """

    def __init__(self, database: Database, db_conn, hs):
        super(StateGroupDataStore, self).__init__(database, db_conn, hs)

        # Originally the state store used a single DictionaryCache to cache the
        # event IDs for the state types in a given state group to avoid hammering
        # on the state_group* tables.
        #
        # The point of using a DictionaryCache is that it can cache a subset
        # of the state events for a given state group (i.e. a subset of the keys for a
        # given dict which is an entry in the cache for a given state group ID).
        #
        # However, this poses problems when performing complicated queries
        # on the store - for instance: "give me all the state for this group, but
        # limit members to this subset of users", as DictionaryCache's API isn't
        # rich enough to say "please cache any of these fields, apart from this subset".
        # This is problematic when lazy loading members, which requires this behaviour,
        # as without it the cache has no choice but to speculatively load all
        # state events for the group, which negates the efficiency being sought.
        #
        # Rather than overcomplicating DictionaryCache's API, we instead split the
        # state_group_cache into two halves - one for tracking non-member events,
        # and the other for tracking member_events.  This means that lazy loading
        # queries can be made in a cache-friendly manner by querying both caches
        # separately and then merging the result.  So for the example above, you
        # would query the members cache for a specific subset of state keys
        # (which DictionaryCache will handle efficiently and fine) and the non-members
        # cache for all state (which DictionaryCache will similarly handle fine)
        # and then just merge the results together.
        #
        # We size the non-members cache to be smaller than the members cache as the
        # vast majority of state in Matrix (today) is member events.

        self._state_group_cache = DictionaryCache(
            "*stateGroupCache*",
            # TODO: this hasn't been tuned yet
            50000 * get_cache_factor_for("stateGroupCache"),
        )
        self._state_group_members_cache = DictionaryCache(
            "*stateGroupMembersCache*",
            500000 * get_cache_factor_for("stateGroupMembersCache"),
        )

    @cached(max_entries=10000, iterable=True)
    def get_state_group_delta(self, state_group):
        """Given a state group try to return a previous group and a delta between
        the old and the new.

        Returns:
            (prev_group, delta_ids), where both may be None.
        """

        def _get_state_group_delta_txn(txn):
            prev_group = self.db.simple_select_one_onecol_txn(
                txn,
                table="state_group_edges",
                keyvalues={"state_group": state_group},
                retcol="prev_state_group",
                allow_none=True,
            )

            if not prev_group:
                return _GetStateGroupDelta(None, None)

            delta_ids = self.db.simple_select_list_txn(
                txn,
                table="state_groups_state",
                keyvalues={"state_group": state_group},
                retcols=("type", "state_key", "event_id"),
            )

            return _GetStateGroupDelta(
                prev_group,
                {(row["type"], row["state_key"]): row["event_id"] for row in delta_ids},
            )

        return self.db.runInteraction(
            "get_state_group_delta", _get_state_group_delta_txn
        )

    @defer.inlineCallbacks
    def _get_state_groups_from_groups(self, groups, state_filter):
        """Returns the state groups for a given set of groups, filtering on
        types of state events.

        Args:
            groups(list[int]): list of state group IDs to query
            state_filter (StateFilter): The state filter used to fetch state
                from the database.
        Returns:
            Deferred[dict[int, dict[tuple[str, str], str]]]:
                dict of state_group_id -> (dict of (type, state_key) -> event id)
        """
        results = {}

        chunks = [groups[i : i + 100] for i in range(0, len(groups), 100)]
        for chunk in chunks:
            res = yield self.db.runInteraction(
                "_get_state_groups_from_groups",
                self._get_state_groups_from_groups_txn,
                chunk,
                state_filter,
            )
            results.update(res)

        return results

    def _get_state_for_group_using_cache(self, cache, group, state_filter):
        """Checks if group is in cache. See `_get_state_for_groups`

        Args:
            cache(DictionaryCache): the state group cache to use
            group(int): The state group to lookup
            state_filter (StateFilter): The state filter used to fetch state
                from the database.

        Returns 2-tuple (`state_dict`, `got_all`).
        `got_all` is a bool indicating if we successfully retrieved all
        requests state from the cache, if False we need to query the DB for the
        missing state.
        """
        is_all, known_absent, state_dict_ids = cache.get(group)

        if is_all or state_filter.is_full():
            # Either we have everything or want everything, either way
            # `is_all` tells us whether we've gotten everything.
            return state_filter.filter_state(state_dict_ids), is_all

        # tracks whether any of our requested types are missing from the cache
        missing_types = False

        if state_filter.has_wildcards():
            # We don't know if we fetched all the state keys for the types in
            # the filter that are wildcards, so we have to assume that we may
            # have missed some.
            missing_types = True
        else:
            # There aren't any wild cards, so `concrete_types()` returns the
            # complete list of event types we're wanting.
            for key in state_filter.concrete_types():
                if key not in state_dict_ids and key not in known_absent:
                    missing_types = True
                    break

        return state_filter.filter_state(state_dict_ids), not missing_types

    @defer.inlineCallbacks
    def _get_state_for_groups(self, groups, state_filter=StateFilter.all()):
        """Gets the state at each of a list of state groups, optionally
        filtering by type/state_key

        Args:
            groups (iterable[int]): list of state groups for which we want
                to get the state.
            state_filter (StateFilter): The state filter used to fetch state
                from the database.
        Returns:
            Deferred[dict[int, dict[tuple[str, str], str]]]:
                dict of state_group_id -> (dict of (type, state_key) -> event id)
        """

        member_filter, non_member_filter = state_filter.get_member_split()

        # Now we look them up in the member and non-member caches
        (
            non_member_state,
            incomplete_groups_nm,
        ) = yield self._get_state_for_groups_using_cache(
            groups, self._state_group_cache, state_filter=non_member_filter
        )

        (
            member_state,
            incomplete_groups_m,
        ) = yield self._get_state_for_groups_using_cache(
            groups, self._state_group_members_cache, state_filter=member_filter
        )

        state = dict(non_member_state)
        for group in groups:
            state[group].update(member_state[group])

        # Now fetch any missing groups from the database

        incomplete_groups = incomplete_groups_m | incomplete_groups_nm

        if not incomplete_groups:
            return state

        cache_sequence_nm = self._state_group_cache.sequence
        cache_sequence_m = self._state_group_members_cache.sequence

        # Help the cache hit ratio by expanding the filter a bit
        db_state_filter = state_filter.return_expanded()

        group_to_state_dict = yield self._get_state_groups_from_groups(
            list(incomplete_groups), state_filter=db_state_filter
        )

        # Now lets update the caches
        self._insert_into_cache(
            group_to_state_dict,
            db_state_filter,
            cache_seq_num_members=cache_sequence_m,
            cache_seq_num_non_members=cache_sequence_nm,
        )

        # And finally update the result dict, by filtering out any extra
        # stuff we pulled out of the database.
        for group, group_state_dict in iteritems(group_to_state_dict):
            # We just replace any existing entries, as we will have loaded
            # everything we need from the database anyway.
            state[group] = state_filter.filter_state(group_state_dict)

        return state

    def _get_state_for_groups_using_cache(self, groups, cache, state_filter):
        """Gets the state at each of a list of state groups, optionally
        filtering by type/state_key, querying from a specific cache.

        Args:
            groups (iterable[int]): list of state groups for which we want
                to get the state.
            cache (DictionaryCache): the cache of group ids to state dicts which
                we will pass through - either the normal state cache or the specific
                members state cache.
            state_filter (StateFilter): The state filter used to fetch state
                from the database.

        Returns:
            tuple[dict[int, dict[tuple[str, str], str]], set[int]]: Tuple of
            dict of state_group_id -> (dict of (type, state_key) -> event id)
            of entries in the cache, and the state group ids either missing
            from the cache or incomplete.
        """
        results = {}
        incomplete_groups = set()
        for group in set(groups):
            state_dict_ids, got_all = self._get_state_for_group_using_cache(
                cache, group, state_filter
            )
            results[group] = state_dict_ids

            if not got_all:
                incomplete_groups.add(group)

        return results, incomplete_groups

    def _insert_into_cache(
        self,
        group_to_state_dict,
        state_filter,
        cache_seq_num_members,
        cache_seq_num_non_members,
    ):
        """Inserts results from querying the database into the relevant cache.

        Args:
            group_to_state_dict (dict): The new entries pulled from database.
                Map from state group to state dict
            state_filter (StateFilter): The state filter used to fetch state
                from the database.
            cache_seq_num_members (int): Sequence number of member cache since
                last lookup in cache
            cache_seq_num_non_members (int): Sequence number of member cache since
                last lookup in cache
        """

        # We need to work out which types we've fetched from the DB for the
        # member vs non-member caches. This should be as accurate as possible,
        # but can be an underestimate (e.g. when we have wild cards)

        member_filter, non_member_filter = state_filter.get_member_split()
        if member_filter.is_full():
            # We fetched all member events
            member_types = None
        else:
            # `concrete_types()` will only return a subset when there are wild
            # cards in the filter, but that's fine.
            member_types = member_filter.concrete_types()

        if non_member_filter.is_full():
            # We fetched all non member events
            non_member_types = None
        else:
            non_member_types = non_member_filter.concrete_types()

        for group, group_state_dict in iteritems(group_to_state_dict):
            state_dict_members = {}
            state_dict_non_members = {}

            for k, v in iteritems(group_state_dict):
                if k[0] == EventTypes.Member:
                    state_dict_members[k] = v
                else:
                    state_dict_non_members[k] = v

            self._state_group_members_cache.update(
                cache_seq_num_members,
                key=group,
                value=state_dict_members,
                fetched_keys=member_types,
            )

            self._state_group_cache.update(
                cache_seq_num_non_members,
                key=group,
                value=state_dict_non_members,
                fetched_keys=non_member_types,
            )

    def store_state_group(
        self, event_id, room_id, prev_group, delta_ids, current_state_ids
    ):
        """Store a new set of state, returning a newly assigned state group.

        Args:
            event_id (str): The event ID for which the state was calculated
            room_id (str)
            prev_group (int|None): A previous state group for the room, optional.
            delta_ids (dict|None): The delta between state at `prev_group` and
                `current_state_ids`, if `prev_group` was given. Same format as
                `current_state_ids`.
            current_state_ids (dict): The state to store. Map of (type, state_key)
                to event_id.

        Returns:
            Deferred[int]: The state group ID
        """

        def _store_state_group_txn(txn):
            if current_state_ids is None:
                # AFAIK, this can never happen
                raise Exception("current_state_ids cannot be None")

            state_group = self.database_engine.get_next_state_group_id(txn)

            self.db.simple_insert_txn(
                txn,
                table="state_groups",
                values={"id": state_group, "room_id": room_id, "event_id": event_id},
            )

            # We persist as a delta if we can, while also ensuring the chain
            # of deltas isn't tooo long, as otherwise read performance degrades.
            if prev_group:
                is_in_db = self.db.simple_select_one_onecol_txn(
                    txn,
                    table="state_groups",
                    keyvalues={"id": prev_group},
                    retcol="id",
                    allow_none=True,
                )
                if not is_in_db:
                    raise Exception(
                        "Trying to persist state with unpersisted prev_group: %r"
                        % (prev_group,)
                    )

                potential_hops = self._count_state_group_hops_txn(txn, prev_group)
            if prev_group and potential_hops < MAX_STATE_DELTA_HOPS:
                self.db.simple_insert_txn(
                    txn,
                    table="state_group_edges",
                    values={"state_group": state_group, "prev_state_group": prev_group},
                )

                self.db.simple_insert_many_txn(
                    txn,
                    table="state_groups_state",
                    values=[
                        {
                            "state_group": state_group,
                            "room_id": room_id,
                            "type": key[0],
                            "state_key": key[1],
                            "event_id": state_id,
                        }
                        for key, state_id in iteritems(delta_ids)
                    ],
                )
            else:
                self.db.simple_insert_many_txn(
                    txn,
                    table="state_groups_state",
                    values=[
                        {
                            "state_group": state_group,
                            "room_id": room_id,
                            "type": key[0],
                            "state_key": key[1],
                            "event_id": state_id,
                        }
                        for key, state_id in iteritems(current_state_ids)
                    ],
                )

            # Prefill the state group caches with this group.
            # It's fine to use the sequence like this as the state group map
            # is immutable. (If the map wasn't immutable then this prefill could
            # race with another update)

            current_member_state_ids = {
                s: ev
                for (s, ev) in iteritems(current_state_ids)
                if s[0] == EventTypes.Member
            }
            txn.call_after(
                self._state_group_members_cache.update,
                self._state_group_members_cache.sequence,
                key=state_group,
                value=dict(current_member_state_ids),
            )

            current_non_member_state_ids = {
                s: ev
                for (s, ev) in iteritems(current_state_ids)
                if s[0] != EventTypes.Member
            }
            txn.call_after(
                self._state_group_cache.update,
                self._state_group_cache.sequence,
                key=state_group,
                value=dict(current_non_member_state_ids),
            )

            return state_group

        return self.db.runInteraction("store_state_group", _store_state_group_txn)

    def purge_unreferenced_state_groups(
        self, room_id: str, state_groups_to_delete
    ) -> defer.Deferred:
        """Deletes no longer referenced state groups and de-deltas any state
        groups that reference them.

        Args:
            room_id: The room the state groups belong to (must all be in the
                same room).
            state_groups_to_delete (Collection[int]): Set of all state groups
                to delete.
        """

        return self.db.runInteraction(
            "purge_unreferenced_state_groups",
            self._purge_unreferenced_state_groups,
            room_id,
            state_groups_to_delete,
        )

    def _purge_unreferenced_state_groups(self, txn, room_id, state_groups_to_delete):
        logger.info(
            "[purge] found %i state groups to delete", len(state_groups_to_delete)
        )

        rows = self.db.simple_select_many_txn(
            txn,
            table="state_group_edges",
            column="prev_state_group",
            iterable=state_groups_to_delete,
            keyvalues={},
            retcols=("state_group",),
        )

        remaining_state_groups = set(
            row["state_group"]
            for row in rows
            if row["state_group"] not in state_groups_to_delete
        )

        logger.info(
            "[purge] de-delta-ing %i remaining state groups",
            len(remaining_state_groups),
        )

        # Now we turn the state groups that reference to-be-deleted state
        # groups to non delta versions.
        for sg in remaining_state_groups:
            logger.info("[purge] de-delta-ing remaining state group %s", sg)
            curr_state = self._get_state_groups_from_groups_txn(txn, [sg])
            curr_state = curr_state[sg]

            self.db.simple_delete_txn(
                txn, table="state_groups_state", keyvalues={"state_group": sg}
            )

            self.db.simple_delete_txn(
                txn, table="state_group_edges", keyvalues={"state_group": sg}
            )

            self.db.simple_insert_many_txn(
                txn,
                table="state_groups_state",
                values=[
                    {
                        "state_group": sg,
                        "room_id": room_id,
                        "type": key[0],
                        "state_key": key[1],
                        "event_id": state_id,
                    }
                    for key, state_id in iteritems(curr_state)
                ],
            )

        logger.info("[purge] removing redundant state groups")
        txn.executemany(
            "DELETE FROM state_groups_state WHERE state_group = ?",
            ((sg,) for sg in state_groups_to_delete),
        )
        txn.executemany(
            "DELETE FROM state_groups WHERE id = ?",
            ((sg,) for sg in state_groups_to_delete),
        )

    @defer.inlineCallbacks
    def get_previous_state_groups(self, state_groups):
        """Fetch the previous groups of the given state groups.

        Args:
            state_groups (Iterable[int])

        Returns:
            Deferred[dict[int, int]]: mapping from state group to previous
            state group.
        """

        rows = yield self.db.simple_select_many_batch(
            table="state_group_edges",
            column="prev_state_group",
            iterable=state_groups,
            keyvalues={},
            retcols=("prev_state_group", "state_group"),
            desc="get_previous_state_groups",
        )

        return {row["state_group"]: row["prev_state_group"] for row in rows}

    def purge_room_state(self, room_id, state_groups_to_delete):
        """Deletes all record of a room from state tables

        Args:
            room_id (str):
            state_groups_to_delete (list[int]): State groups to delete
        """

        return self.db.runInteraction(
            "purge_room_state",
            self._purge_room_state_txn,
            room_id,
            state_groups_to_delete,
        )

    def _purge_room_state_txn(self, txn, room_id, state_groups_to_delete):
        # first we have to delete the state groups states
        logger.info("[purge] removing %s from state_groups_state", room_id)

        self.db.simple_delete_many_txn(
            txn,
            table="state_groups_state",
            column="state_group",
            iterable=state_groups_to_delete,
            keyvalues={},
        )

        # ... and the state group edges
        logger.info("[purge] removing %s from state_group_edges", room_id)

        self.db.simple_delete_many_txn(
            txn,
            table="state_group_edges",
            column="state_group",
            iterable=state_groups_to_delete,
            keyvalues={},
        )

        # ... and the state groups
        logger.info("[purge] removing %s from state_groups", room_id)

        self.db.simple_delete_many_txn(
            txn,
            table="state_groups",
            column="id",
            iterable=state_groups_to_delete,
            keyvalues={},
        )
