###############################################################################
#
# Copyright (C) 2017 Andrew Muzikin
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################

# import numpy as np
import tensorflow as tf


class BTgymReplayMemory():
    """
    Sequential/random access replay memory class for Rl agents
    with multi-modal experience and observation state shapes
    and episodic tasks with known maximum length of the episode.
    Stores entire memory as nested dictionary of tf.variables,
    can be saved and restored as part of the model.

    One memory record is `experience` dictionary, defined by `experience_shape`.
    Memory itself consists up to `memory_shape[0]` number of episodes,
    and an episode is just an ordered sequence of experiences
    with maximum length defined by `memory_shape[1]` value.
    Due to this fact it is possible to extract agent experience in form of [S, A, R, S']
    for every but initial episode step, as well as in traces.

    Experience_shape format:
        can be [nested] dictionary of any structure
        with at least these keys presented at top-level:
            `action`,
            `reward`,
            `done`,
            `state_next`;
        every end-level record is tuple describing tf.variable shape and dtype.
        Shape is arbitrary, dtype can be any of valid tf.Dtype's. I dtype arg is omitted,
        tf.float32 will be set by default.

    Example:
        robo_experience_shape = dict(
            action=(4,tf.uint8),  # unsigned 8bit integer vector
            reward=(),  # float32 by default, scalar
            done=(tf.bool,),   # boolean, scalar
            state_next=dict(
                internal=dict(
                    hardware=dict(
                        battery=(),  # float32, scalar
                        oil_pressure=(3,),  # got 3 pumps, float32, vector
                        tyre_pressure=(4,),  # for each one, float32, vector
                        checks_passed=(tf.bool,)  # boolean, scalar
                    ),
                    psyche=dict(
                        optimism=(tf.int32,),  # can be high, 32bit int, scalar
                        sincerity=(),  # float32 by default, scalar
                        message=(4,tf.string,),  # knows four phrases
                    )
                ),
                external=dict(
                    camera=(2,180,180,3,tf.uint8),  # binocular rgb 180x180 image, unsigned 8bit integers
                    audio_sensor=(2,320,)  # stereo audio sample buffer, float32
                ),
            ),
            global_training_day=(uint16,)  # just curious how long it took to get to this state.
        )
    """

    def __init__(self,
                 experience_shape,  # nested dictionary containing single experience definition.
                 max_episode_length,  # in number of steps
                 max_size=100000,  # in number of experiences
                 batch_size=32,  # sampling batch size
                 scope='replay_memory',):
        """______"""
        self.experience_shape = experience_shape
        self.max_episode_length = max_episode_length
        self.batch_size = batch_size
        self.memory_shape = (int(max_size / max_episode_length), self.max_episode_length)
        self.mandatory_keys = ['state_next', 'action', 'reward', 'done']

        # Check experience_shape consistency:
        for key in self.mandatory_keys:
            if key not in self.experience_shape:
                msg = (
                    'Mandatory key [{}] not found at top level of `memory.experience_shape` dictionary.\n' +\
                    'Hint: `memory.mandatory_keys` are {}.'
                ).format(key, self.mandatory_keys)
                raise ValueError(msg)

        # Check size consistency:
        try:
            assert self.memory_shape[0] > 0

        except:
            raise ValueError('Memory maximum size <{}> is smaller than maximum single episode length <{}>.:'.
                format(max_size, self.max_episode_length))

        self.local_step = 0  # step within current episode
        self.episode = 0  # keep track of episode numbers within current tf.Session()
        self.current_mem_size = 0  # [points to] stateful tf.variable
        self.current_mem_pointer = -1  # [points to] stateful tf.variable

        # Build logic:
        with tf.variable_scope(scope):
            self._global_variable_constructor()
            self._tf_graph_constructor()

    def _global_variable_constructor(self):
        """
        Defines TF variables and placeholders.
        """
        with tf.variable_scope('service'):
            # Stateful memory variables:
            self._mem_current_size = tf.Variable(  # in number of episodes
                0,
                trainable=False,
                name='current_size',
                dtype=tf.int32,
            )
            self._mem_cyclic_pointer = tf.Variable(
                0,
                trainable=False,
                name='cyclic_pointer',
                dtype=tf.int32,
            )
            # Indices for retrieving batch of experiences:
            self._batch_indices = tf.Variable(
                tf.zeros(shape=(self.batch_size, 2), dtype=tf.int32),
                trainable=False,
                name='batch_experience_indices',
                dtype=tf.int32,
            )
        # Memory  itself  (as nested dictionary of tensors):
        self.memory = self._var_constructor(
            self.experience_shape,
            self.memory_shape,
            scope='field',
        )
        # Add at top-level:
        self.memory['episode_length'] = tf.Variable(
            tf.ones((self.memory_shape[0],), dtype=tf.int32),
            trainable=False,
            name='field/episode_length',
            dtype=tf.int32,
        )
        # Memory input buffer, accumulates experiences of single episode.
        self.buffer = self._var_constructor(
            self.experience_shape,
            (self.max_episode_length,),
            scope='buffer',
        )
        with tf.variable_scope('placeholder'):
            # Placeholders to feed single experience to mem. buffer:
            self.buffer_pl = self._buffer_pl_constructor(
                self.buffer,
                scope='buffer',
            )
            # Placeholders for service variables:
            self._mem_current_size_pl = tf.placeholder(
                dtype=tf.int32,
            )
            self._mem_cyclic_pointer_pl = tf.placeholder(
                dtype=tf.int32,
            )
            self._local_step_pl = tf.placeholder(
                dtype=tf.int32,
            )
            self._mem_pointer1_pl = tf.placeholder(
                dtype=tf.int32,
            )
            self._mem_pointer2_pl = tf.placeholder(
                dtype=tf.int32,
            )
            self._local_step_pl = tf.placeholder(
                dtype=tf.int32,
            )
            self._indices1_pl = tf.placeholder(
                dtype=tf.int32,
            )
            self._indices2_pl = tf.placeholder(
                dtype=tf.int32,
            )

    def _var_constructor(self, shape_dict, memory_shape, scope='record'):
        """
        Recursive tf.variable constructor.
        Takes:
            shape_dict:
                nested dictionary of tuples in form:
                key_name=(dim_0, dim_1,..., dim_N, [tf.dtype]);
                opt. dtype must be one of tf.DType class object, see:
                https://www.tensorflow.org/api_docs/python/tf/DType
                by default (if no dtype arg. present) is set to: tf.float32;
            memory_shape:
                tuple (memory_size_in_episodes, max_length_of_single_episode);
            scope:
                top-level name scope.
        Returns:
            nested dictionary of tf.variables of same structure, where every `key` tf.variable has
            name:
                'full_nested_scope/key:0';
            shape:
                (memory_shape[0],...,memory_shape[-1], key_dim_0,..., key_dim_[-1]);
            type:
                consistent tf.Dtype.
        """
        var_dict = dict()
        for key, record in shape_dict.items():
            if type(record) == dict:
                var_dict[key] = self._var_constructor(record, memory_shape, '{}/{}'.format(scope, str(key)))
            else:
                # If dtype is not present - set it to tf.float32
                dtype = tf.float32
                if len(record) > 0 and type(record[-1]) != int:
                    dtype = record[-1]
                    record = record[0:-1]
                var_dict[key] = tf.Variable(
                    tf.zeros(memory_shape + record, dtype=dtype),
                    trainable=False,
                    name='{}/{}'.format(scope, str(key)),
                    dtype=dtype,
                )
        return var_dict

    def _pl_constructor(self, var_dict, scope='placeholder'):
        """
        Recursive placeholder constructor.
        Takes:
            var_dict:
                nested dictionary of tf.variables;
            scope:
                top-level name scope.
        Returns:
            nested dictionary of placeholders compatible with `var_dict`.
        """
        feed_dict = dict()
        for key, record in var_dict.items():
            if type(record) == dict:
                feed_dict[key] = self._pl_constructor(
                    record,
                    '{}/{}'.format(scope, str(key)),
                )
            else:
                feed_dict[key] = tf.placeholder(
                    dtype=record.dtype,
                    shape=record.shape,
                    name='{}/{}'.format(scope, str(key)),
                )
        return feed_dict

    def _buffer_pl_constructor(self, buffer_dict, scope='buffer_pl'):
        """
        Defines placeholders to feed single experience to memory buffer.
        Takes:
            buffer_dict:
                nested dictionary of tf.variables;
           scope:
                top-level name scope.
        Returns:
            nested dictionary of placeholders compatible with `buffer_dict`, with
            rank of each placeholder reduced by one in expense of removing (episode_length) dimension.
        """
        feed_dict = dict()
        for key, record in buffer_dict.items():
            if type(record) == dict:
                feed_dict[key] = self._buffer_pl_constructor(
                    record,
                    '{}/{}'.format(scope, str(key)),
                )
            else:
                feed_dict[key] = tf.placeholder(
                    dtype=record.dtype,
                    shape=record.shape[1:],
                    name='{}/{}'.format(scope, str(key)),
                )
        return feed_dict

    def _feed_buffer_op_constructor(self, var_dict, pl_dict, step_pl, scope='add_experience'):
        """
        Defines operations to store single experience in memory buffer.
        Takes:
            var_dict:
                nested dictionary of tf.variables;
            pl_dict:
                nested dictionary of placeholders of consisted structure.
            step_pl:
                local step of the episode, scalar;
            scope:
                top-level name scope.
        Returns:
            nested dictionary of `tf.assign` operations.
        """
        op_dict = dict()
        for key, var in var_dict.items():
            if type(var) == dict:
                op_dict[key] = self._feed_buffer_op_constructor(
                    var,
                    pl_dict[key],
                    step_pl,
                    '{}/{}'.format(scope, str(key)),
                )
            else:
                op_dict[key] = tf.assign(
                    var[step_pl, ...],
                    pl_dict[key],
                    name='{}/{}'.format(scope, str(key)),
                )
        return op_dict

    def _feed_episode_op_constructor(self, memory_dict, buffer_dict, position_pl, length_pl, scope='add_episode'):
        """
        Defines operations to store single episode to memory.
        Takes:
            memory_dict:
                nested dictionary of tf.variables;
            pl_dict:
                nested dictionary of placeholders of same as `var_dict` structure;
            position_pl:
                place in memory to write episode to, scalar;
            length_pl:
                episode length, scalar.
            scope:
                top-level name scope.
        Returns:
            nested dictionary of operations.
        """
        op_dict = dict()
        for key, var in memory_dict.items():
            if type(var) == dict:
                op_dict[key] = self._feed_episode_op_constructor(
                    var,
                    buffer_dict[key],
                    position_pl,
                    length_pl,
                    '{}/{}'.format(scope, str(key)),
                )
            else:
                if key == 'episode_length':
                    op_dict[key] = tf.assign(
                        var[position_pl, ...],
                        length_pl,
                        name='{}/{}'.format(scope, str(key)),
                    )
                else:
                    op_dict[key] = tf.assign(
                        var[position_pl, ...],
                        buffer_dict[key],
                        name='{}/{}'.format(scope, str(key)),
                    )
        return op_dict

    def _get_episode_op_constructor(self, memory_dict, position_pl, episode_length=None,):
        """
        Defines ops to retrieve single episode from memory.
        Takes:
            memory_dict:
                nested dictionary of tf.variables;
            position_pl:
                place in memory to get episode from, scalar;
        Returns:
            nested dictionary of sliced tensors.
        """
        get_dict=dict()
        if episode_length is None:
            episode_length = memory_dict['episode_length'][position_pl]
        for key, var in memory_dict.items():
            if type(var) == dict:
                get_dict[key] = self._get_episode_op_constructor(
                    var,
                    position_pl,
                    episode_length,
                )
            else:
                if key != 'episode_length':
                    get_dict[key] = memory_dict[key][position_pl, 0:episode_length, ...]
                else:
                    get_dict[key] = memory_dict[key][position_pl, ...]
        return get_dict

    def _get_experience_op_constructor(self, memory_dict, position1_pl, position2_pl,):
        """
        Defines ops to retrieve single experience from memory.
        Takes:
            memory_dict:
                nested dictionary of tf.variables;
            position1_pl:
                place of episode in memory to get experience from, scalar;
            position2_pl:
                place of experience within episode (i.e. step), scalar;
        Returns:
            nested dictionary of sliced tensors.
        """
        get_dict = dict()
        for key, var in memory_dict.items():
            if type(var) == dict:
                get_dict[key] = self._get_experience_op_constructor(
                    var,
                    position1_pl,
                    position2_pl,
                )
            else:
                if key != 'episode_length':
                    get_dict[key] = memory_dict[key][position1_pl, position2_pl, ...]
        return get_dict

    def _get_experience_batch_op_constructor(self, memory_dict, batch_indices):
        """
        Defines operations to retrieve batch of experiences from memory.
        Takes:
            memory_dict:
                nested dictionary of tf.variables;
            batch_indices:
                rank2 tensor of indices as: [batch_size] x [episode_number, episode_step].
        Returns:
            nested dictionary of sliced tensors.
        """
        batch_dict = dict()
        for key, var in memory_dict.items():
            if key != 'episode_length':
                if type(var) == dict:
                    batch_dict[key] = self._get_experience_batch_op_constructor(
                        var,
                        batch_indices,
                    )
                else:
                    batch_dict[key] =  tf.gather_nd(
                        memory_dict[key],
                        batch_indices,
                    )
        return batch_dict

    def _sample_indices_batch_op_constructor(self):
        """
        Defines operations for sampling random batch of experiences's indices.
        Returns:
            index of experiences in shape [batch_size] x [num_episode] x [num_experience],
            also stored in `_batch_indices` tf.variable.
        Note:
            initial experiences are excluded from sampling range.
        """
        # Sample episode numbers:
        episodes_indices = self._batch_indices[:, 0].assign(
            tf.random_uniform(
                shape=(self.batch_size,),
                minval=1,
                maxval=self._mem_current_size,
                dtype=tf.int32,
            )
        )
        # Get real length value for each sampled episode:
        episode_len_values = tf.gather(
            self.memory['episode_length'],
            episodes_indices[:, 0],
        )
        # Now can sample experiences indices:
        sample_idx = self._batch_indices[:, 1].assign(
            tf.cast(
                tf.multiply(
                    tf.cast(
                        episode_len_values - 1,
                        dtype=tf.float32,
                    ),
                    tf.random_uniform(
                        shape=(self.batch_size,),
                        minval=0,
                        maxval=1,
                        dtype=tf.float32,
                    )
                ),
                dtype=tf.int32,
            ) + 1
        )
        return sample_idx

    def _get_sars_batch_op_constructor(self, batch_indices):
        """
        Defines operations for getting batch of experiences in S,A,R,S` form.
        Returns:
            dictionary of operations.
        """
        # Get `-1` indices for `state` field:
        previous_mask = tf.stack(
            [
                tf.zeros(shape=(self.batch_size,), dtype=tf.int32),
                tf.ones(shape=(self.batch_size,), dtype=tf.int32),
            ],
            axis=1,
        )
        batch_indices_previous = tf.subtract(
            batch_indices,
            previous_mask,
        )
        # Get -A,R,S` part:
        sars_batch = self._get_experience_batch_op_constructor(
            self.memory,
            batch_indices,
        )
        # Get S,- part:
        sars_batch['state'] = self._get_experience_batch_op_constructor(
            self.memory['state_next'],
            batch_indices_previous,
        )
        return sars_batch

    def _make_feeder(self, pl_dict, value_dict):
        """
        Makes `serialized` feed dictionary.
        Takes:
            pl_dict:
                nested dictionary of tf.placeholders;
            value_dict:
                dictionary of values of same as `pl_dict` structure.
        Returns:
            flattened feed dictionary, tf.Session.run()-ready.
        """
        feeder = dict()
        for key, record in pl_dict.items():
            if type(record) == dict:
                feeder.update(self._make_feeder(record, value_dict[key]))
            else:
                feeder.update({record: value_dict[key]})
        return feeder

    def _tf_graph_constructor(self):
        """
        Defines TF graphs and it's handles.
        """
        # Set memory pointers:
        self._set_mem_cyclic_pointer_op = self._mem_cyclic_pointer.assign(self._mem_cyclic_pointer_pl),
        self._set_mem_current_size_op = self._mem_current_size.assign(self._mem_current_size_pl),

        # Add single experience to buffer:
        self._add_experience_op = self._feed_buffer_op_constructor(
            self.buffer,
            self.buffer_pl,
            self._local_step_pl,
            scope='add_experience/',
        )
        # Store single episode in memory:
        self._save_episode_op = self._feed_episode_op_constructor(
            self.memory,
            self.buffer,
            self._mem_cyclic_pointer,  # can??
            self._local_step_pl,
            scope='add_episode/',
        )
        # Get single episode from memory:
        self._get_episode_op = self._get_episode_op_constructor(
            self.memory,
            self._mem_pointer1_pl,
        )
        # Get single experience from memory:
        self._get_experience_op = self._get_experience_op_constructor(
            self.memory,
            self._mem_pointer1_pl,
            self._mem_pointer2_pl,
        )
        # Gather episode's length values by its numbers:
        self._get_episodes_length_op = tf.gather(self.memory['episode_length'], self._indices1_pl)

        # Sample batch of random indices:
        self._sample_batch_indices_op = self._sample_indices_batch_op_constructor()

        # Get batch of sampled S,A,R,S` experiences(i.e. stored in `self._batch_indices` variable):
        self._get_sampled_sars_batch_op = self._get_sars_batch_op_constructor(self._batch_indices)

    def _evaluate_buffer(self, buffer_dict, sess):
        """
        Handy if something goes wrong.
        """
        content_dict = dict()
        for key, var in buffer_dict.items():
            if type(var) == dict:
                content_dict[key] = self._evaluate_buffer(var, sess)
            else:
                content_dict[key] = sess.run(var)
        return content_dict

    def _print_nested_dict(self, nested_dict, tab=''):
        """
        Handy.
        """
        for k, v in nested_dict.items():
            if type(v) == dict:
                print('{}{}:'.format(tab, k))
                self._print_nested_dict(v, tab + '   ')
            else:
                print('{}{}:'.format(tab, k))
                print('{}{}'.format(tab + tab, v))

    def _print_global_variables(self):
        """
        Handy.
        """
        for v in tf.global_variables():
            print(v)

    def _get_current_size(self, sess):
        """
        Returns current used memory size
        in number of stored episodes, in range: [0, max_mem_size).
        """
        # TODO: maybe .eval?
        return sess.run(self._mem_current_size)

    def _set_current_size(self, sess, value):
        """
        Sets current used memory size in number of episodes.
        """
        assert value <= self.memory_shape[0]
        _ = sess.run(
            self._set_mem_current_size_op,
            feed_dict={
                self._mem_current_size_pl: value,
            }
        )

    def _get_cyclic_pointer(self, sess):
        """
        Cyclic pointer stores number (==address) of episode in replay memory,
        currently to be written/replaced.
        This pointer supposed to infinitely loop through entire memory, updating records.
        Returns:
            current pointer value.
        """
        return sess.run(self._mem_cyclic_pointer)

    def _set_cyclic_pointer(self, sess, value):
        """
        Sets one.
        """
        _ = sess.run(self._set_mem_cyclic_pointer_op,
                     feed_dict={self._mem_cyclic_pointer_pl: value},
                     )

    def update(self, sess, experience):
        """
        Sintax shugar for adding single experience to memory.
        """
        self._add_experience(sess, experience)

    def _add_experience(self, sess, experience):
        """
        Writes single experience to episode memory buffer and
        calls add_episode() method if experience['done']=True
        or maximum memory episode length exceeded.
        Receives:
            sess:       tf.Session object,
            experience: dictionary containing agent experience,
                        shaped according to self.experience_shape.
        """
        # Get 'done' flag:
        done = experience['done']

        # Prepare feeder dict:
        feeder = self._make_feeder(
            pl_dict=self.buffer_pl,
            value_dict=experience,
        )
        # Add local step:
        feeder.update({self._local_step_pl: self.local_step})

        # Save it:
        _ = sess.run(
            self._add_experience_op,
            feed_dict=feeder,
        )
        if done or self.local_step >= self.memory_shape[1]:
            # If over, store episode in replay memory:
            self._add_episode(sess)
        else:
            self.local_step += 1

    def _add_episode(self, sess):
        """
        Writes episode to replay memory.
        """
        # Save:
        _ = sess.run(
            self._save_episode_op,
            feed_dict={
                self._local_step_pl: self.local_step + 1,
            }
        )
        # Reset local_step, increment episode count:
        self.local_step = 0
        self.episode += 1

        # Increment memory size and move cycle_pointer to next episode:
        # Get actual size and pointer:
        self.current_mem_size = self._get_current_size(sess)
        self.current_mem_pointer = self._get_cyclic_pointer(sess)

        if self.current_mem_size < self.memory_shape[0] - 1:
            # If memory is not full - increase used size by 1,
            # else - leave it along:
            self.current_mem_size += 1
            self._set_current_size(sess, self.current_mem_size)

        if self.current_mem_pointer >= self.current_mem_size:
            # Rewind cyclic pointer, if reached memory upper bound:
            self._set_cyclic_pointer(sess, 0)
            self.current_mem_pointer = 0

        else:
            # Increment:
            self.current_mem_pointer += 1
            self._set_cyclic_pointer(sess, self.current_mem_pointer)

    def get_episode(self, sess, episode_number):
        """
        Retrieves single episode from memory.
        Returns:
            dictionary with keys defined by `experience_shape`,
            containing episode records.
        """
        try:
            assert episode_number <= self._get_current_size(sess)

        except:
            raise ValueError('Episode index <{}> is out of memory bounds <{}>.'.
                             format(episode_number, self._get_current_size(sess)))

        episode_dict = sess.run(
                self._get_episode_op,
                feed_dict={
                    self._mem_pointer1_pl: episode_number,
                }
        )
        return episode_dict

    def sample_trace_batch(self, sess):
        raise NotImplementedError

    def sample_random_batch(self, sess):
        """
        Samples batch of random experiences from replay memory.
        This method is stateful: every call will return new sample.
        Returns:
            nested dictionary, holding batches of corresponding memory field experiences:
            S, A, R, S-next, each one is np.array of shape [batch_size] x [field_own_dimension].
        """
        # TODO: can return dict of tensors itself, for direct connection with estimator input.
        self.current_mem_size = self._get_current_size(sess)

        try:
            assert self.batch_size <= self.current_mem_size

        except:
            raise AssertionError(
                'Requested memory batch of size {} can not be sampled: memory contains {} episodes.'.
                format(self.batch_size, self.current_mem_size)
            )
        # Sample:
        idx = sess.run(self._sample_batch_indices_op)
        # Retrieve:
        output_feeder = sess.run(self._get_sampled_sars_batch_op)

        return idx, output_feeder

