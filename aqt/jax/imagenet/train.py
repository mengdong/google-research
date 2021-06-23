# coding=utf-8
# Copyright 2021 The Google Research Authors.
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

"""ImageNet example with optional quantization.

This script trains a ResNet model on the ImageNet dataset.
The data is loaded using tensorflow_datasets.
The function create_model() can accept an hparam file as input, which will
  determine the model's size, training parameters, quantization precisions, etc.
"""

import enum
import functools
import json
import os
import time

from absl import app
from absl import flags
from absl import logging
from flax import jax_utils
from flax import optim
from flax.metrics import tensorboard
from flax.training import checkpoints
from flax.training import common_utils
import jax
from jax import random
import jax.nn
import jax.numpy as jnp
from ml_collections import config_flags
import tensorflow.compat.v2 as tf


from aqt.jax import compute_cost_utils
from aqt.jax import hlo_utils
from aqt.jax import train_utils
from aqt.jax.imagenet import hparams_config
from aqt.jax.imagenet import input_pipeline
from aqt.jax.imagenet import models
from aqt.jax.imagenet import train_utils as imagenet_train_utils
from aqt.utils import hparams_utils as os_hparams_utils
from aqt.utils import report_utils
from aqt.utils import summary_utils


FLAGS = flags.FLAGS


class LRScheduler(enum.Enum):
  """Specifies the learning rate scheduler and decay."""
  STEP = enum.auto()
  COSINE = enum.auto()


flags.DEFINE_string(
    'model_dir', default=None, help=('Directory to store model data.'))

flags.DEFINE_string(
    'data_dir', default=None, help=('Directory where imagenet data is stored.'))

config_flags.DEFINE_config_file('hparams_config_dict', None,
                                'Path to file defining a config dict.')

flags.DEFINE_integer(
    'config_idx',
    default=None,
    help=(
        'Identifies which config within the sweep this training run should use.'
    ))

flags.DEFINE_integer(
    'batch_size', default=128, help=('Batch size for training.'))

flags.DEFINE_bool('cache', default=False, help=('If True, cache the dataset.'))

flags.DEFINE_integer(
    'num_epochs', default=250, help=('Number of training epochs.'))


flags.DEFINE_enum_class(
    'lr_scheduler',
    default=LRScheduler.COSINE,
    enum_class=LRScheduler,
    help=('Learning rate scheduler. Can be set to COSINE or STEP. '
          'COSINE: Cosine decay, tuned to achieve target eval acc quickly. '
          'STEP: Piecewise step decay, uses constant lr at every interval,'
          ' allowing loss to flatten out at each lr, and exponential'
          ' decay of lr throughout the intervals.'))

flags.DEFINE_bool(
    'half_precision',
    default=True,
    help=('If bfloat16/float16 should be used instead of float32.'))


flags.DEFINE_integer(
    'state_dict_summary_freq',
    default=200,
    help='Number of training steps between state dict summaries reported to '
    'Tensorboard. Relevant to --visualize_acts_bound and --collect_acts_stats.')

flags.DEFINE_float(
    'step_lr_coeff',
    default=0.2,
    help='Coefficient used by the step lr scheduler to decay at the end of'
    ' every interval.')

flags.DEFINE_integer(
    'step_lr_intervals',
    default=6,
    help='Number of intervals in the step lr scheduler')

flags.DEFINE_bool(
    'visualize_acts_bound',
    default=True,
    help=(
        'Whether to visualize activations bounds for auto-clip in Tensorboard.'
        ' The bounds appear as scalar and will be named as "GetBounds_0/bounds"'
        ' prefixed with the all the parents module name.'))


def cosine_decay(base_learning_rate, step, decay_steps, alpha=0.001):
  ratio = jnp.minimum(jnp.maximum(0., step / decay_steps), 1.)
  decay = 0.5 * (1. + jnp.cos(jnp.pi * ratio))
  decayed = (1 - alpha) * decay + alpha
  return decayed * base_learning_rate


def step_decay(lr, step, interval):
  """Constant LR within every interval, exponential decay at the end of each."""
  decays = (step // interval) - (step >= interval) + (step >= 2 * interval)
  lr *= FLAGS.step_lr_coeff**decays
  return lr


def create_learning_rate_fn(base_learning_rate, steps_per_epoch, num_epochs):
  """Create learning rate scheduler function."""
  warmup_epochs = 5
  warmup_steps = warmup_epochs * steps_per_epoch
  cooldown_epochs = 50
  cooldown_steps = cooldown_epochs * steps_per_epoch
  total_steps = num_epochs * steps_per_epoch

  def step_fn(step):
    if FLAGS.lr_scheduler == LRScheduler.COSINE:
      lr = cosine_decay(base_learning_rate, step - warmup_steps,
                        total_steps - warmup_steps - cooldown_steps)
    elif FLAGS.lr_scheduler == LRScheduler.STEP:
      epoch = step / steps_per_epoch
      lr = step_decay(base_learning_rate, epoch,
                      num_epochs // FLAGS.step_lr_intervals)
    else:
      raise ValueError('Invalid learning rate scheduler.')
    if warmup_steps > 0:
      warmup = jnp.minimum(1., step / warmup_steps)
      lr *= warmup
    if cooldown_steps > 0:
      cooldown = jnp.minimum(1., (total_steps - step) / cooldown_steps)
      lr *= cooldown
    return lr

  return step_fn


# flax.struct.dataclass enables instances of this class to be passed into jax
# transformations like tree_map and pmap.
def estimate_compute_and_memory_cost(image_size, model_dir, hparams):
  """Estimate compute and memory cost of model."""
  FLAGS.metadata_enabled = True
  input_shape = (1, image_size, image_size, 3)
  model, init_state = imagenet_train_utils.create_model(
      jax.random.PRNGKey(0),
      input_shape[0],
      input_shape[1],
      jnp.float32,
      hparams.model_hparams,
      train=False)
  hlo_proto = hlo_utils.load_hlo_proto_from_model(model, init_state,
                                                  [input_shape])
  del model, init_state
  cost_dict = compute_cost_utils.estimate_compute_cost(hlo_proto)
  memory_cost_dict = compute_cost_utils.estimate_memory_cost(hlo_proto)
  cost_dict.update(memory_cost_dict)
  FLAGS.metadata_enabled = False


  path = os.path.join(model_dir, COMPUTE_MEMORY_COST_FILENAME)
  with open(path, 'w') as file:
    json.dump(cost_dict, file, indent=2)
  logging.info('Estimated compute and memory costs and wrote to file')


def restore_checkpoint(state):
  return checkpoints.restore_checkpoint(FLAGS.model_dir, state)


def save_checkpoint(state):
  if jax.host_id() == 0:
    # get train state from the first replica
    state = jax.device_get(jax.tree_map(lambda x: x[0], state))
    step = int(state.step)
    checkpoints.save_checkpoint(FLAGS.model_dir, state, step, keep=3)


def _get_state_dict_keys_from_flags():
  """Returns key suffixes to look up in flax state dict."""
  state_dict_keys = []
  if FLAGS.visualize_acts_bound:
    state_dict_keys.append('bounds')
  return state_dict_keys



def main(argv):
  if len(argv) > 1:
    raise app.UsageError('Too many command-line arguments.')

  tf.enable_v2_behavior()
  # make sure tf does not allocate gpu memory
  tf.config.experimental.set_visible_devices([], 'GPU')

  if jax.host_id() == 0:
    summary_writer = tensorboard.SummaryWriter(FLAGS.model_dir)

  rng = random.PRNGKey(0)

  image_size = 224

  batch_size = FLAGS.batch_size
  if batch_size % jax.device_count() > 0:
    raise ValueError('Batch size must be divisible by the number of devices')
  local_batch_size = batch_size // jax.host_count()
  device_batch_size = batch_size // jax.device_count()

  platform = jax.local_devices()[0].platform

  dynamic_scale = None
  if FLAGS.half_precision:
    if platform == 'tpu':
      model_dtype = jnp.bfloat16
      input_dtype = tf.bfloat16
    else:
      model_dtype = jnp.float16
      input_dtype = tf.float16
      dynamic_scale = optim.DynamicScale()
  else:
    model_dtype = jnp.float32
    input_dtype = tf.float32

  train_iter = imagenet_train_utils.create_input_iter(
      local_batch_size,
      FLAGS.data_dir,
      image_size,
      input_dtype,
      train=True,
      cache=FLAGS.cache)
  eval_iter = imagenet_train_utils.create_input_iter(
      local_batch_size,
      FLAGS.data_dir,
      image_size,
      input_dtype,
      train=False,
      cache=FLAGS.cache)

  num_epochs = FLAGS.num_epochs
  steps_per_epoch = input_pipeline.TRAIN_IMAGES // batch_size
  steps_per_eval = input_pipeline.EVAL_IMAGES // batch_size
  steps_per_checkpoint = steps_per_epoch * 10
  num_steps = steps_per_epoch * num_epochs

  # Create the hyperparameter object
  if FLAGS.hparams_config_dict:
    # In this case, there are multiple training configs defined in the config
    # dict, so we pull out the one this training run should use.
    if 'configs' in FLAGS.hparams_config_dict:
      hparams_config_dict = FLAGS.hparams_config_dict.configs[FLAGS.config_idx]
    else:
      hparams_config_dict = FLAGS.hparams_config_dict
    hparams = os_hparams_utils.load_hparams_from_config_dict(
        hparams_config.TrainingHParams, models.ResNet.HParams,
        hparams_config_dict)
  else:
    raise ValueError('Please provide a base config dict.')

  os_hparams_utils.write_hparams_to_file_with_host_id_check(
      hparams, FLAGS.model_dir)

  # Estimate compute / memory costs
  if jax.host_id() == 0:
    estimate_compute_and_memory_cost(
        image_size=image_size, model_dir=FLAGS.model_dir, hparams=hparams)
    logging.info('Writing training HLO and estimating compute/memory costs.')

  model, variables = imagenet_train_utils.create_model(
      rng,
      device_batch_size,
      image_size,
      model_dtype,
      hparams=hparams.model_hparams,
      train=True)
  model_state, params = variables.pop('params')
  optimizer = optim.Momentum(
      beta=hparams.momentum, nesterov=True).create(params)
  state = imagenet_train_utils.TrainState(
      step=0,
      optimizer=optimizer,
      model_state=model_state,
      dynamic_scale=dynamic_scale)
  del params, model_state  # do not keep a copy of the initial model

  state = restore_checkpoint(state)
  step_offset = int(state.step)  # step_offset > 0 if restarting from checkpoint
  state = jax_utils.replicate(state)

  base_learning_rate = hparams.base_learning_rate * batch_size / 256.
  learning_rate_fn = create_learning_rate_fn(base_learning_rate,
                                             steps_per_epoch, num_epochs)

  p_train_step = jax.pmap(
      functools.partial(
          imagenet_train_utils.train_step,
          model,
          learning_rate_fn=learning_rate_fn),
      axis_name='batch',
      static_broadcasted_argnums=(2, 3))
  p_eval_step = jax.pmap(
      functools.partial(imagenet_train_utils.eval_step, model),
      axis_name='batch')

  epoch_metrics = []
  state_dict_summary_all = []
  state_dict_keys = _get_state_dict_keys_from_flags()
  t_loop_start = time.time()
  for step, batch in zip(range(step_offset, num_steps), train_iter):
    update_bounds = train_utils.should_update_bounds(
        hparams.activation_bound_update_freq,
        hparams.activation_bound_start_step, step)
    state, metrics = p_train_step(state, batch, hparams, update_bounds)

    state_dict_summary = summary_utils.get_state_dict_summary(
        state.model_state, state_dict_keys)
    state_dict_summary_all.append(state_dict_summary)

    epoch_metrics.append(metrics)
    if (step + 1) % steps_per_epoch == 0:
      epoch = step // steps_per_epoch
      epoch_metrics = common_utils.get_metrics(epoch_metrics)
      summary = jax.tree_map(lambda x: x.mean(), epoch_metrics)
      logging.info('train epoch: %d, loss: %.4f, accuracy: %.2f', epoch,
                   summary['loss'], summary['accuracy'] * 100)
      steps_per_sec = steps_per_epoch / (time.time() - t_loop_start)
      t_loop_start = time.time()

      # Write to TensorBoard
      state_dict_summary_all = common_utils.get_metrics(state_dict_summary_all)
      if jax.host_id() == 0:
        for key, vals in epoch_metrics.items():
          tag = 'train_%s' % key
          for i, val in enumerate(vals):
            summary_writer.scalar(tag, val, step - len(vals) + i + 1)
        summary_writer.scalar('steps per second', steps_per_sec, step)

        summary_utils.write_state_dict_summaries_to_tb(
            state_dict_summary_all, summary_writer,
            FLAGS.state_dict_summary_freq, step)

      state_dict_summary_all = []
      epoch_metrics = []
      eval_metrics = []

      # sync batch statistics across replicas
      state = imagenet_train_utils.sync_batch_stats(state)
      for _ in range(steps_per_eval):
        eval_batch = next(eval_iter)
        metrics = p_eval_step(state, eval_batch)
        eval_metrics.append(metrics)
      eval_metrics = common_utils.get_metrics(eval_metrics)
      summary = jax.tree_map(lambda x: x.mean(), eval_metrics)
      logging.info('eval epoch: %d, loss: %.4f, accuracy: %.2f', epoch,
                   summary['loss'], summary['accuracy'] * 100)
      if jax.host_id() == 0:
        for key, val in eval_metrics.items():
          tag = 'eval_%s' % key
          summary_writer.scalar(tag, val.mean(), step)
        summary_writer.flush()
    if (step + 1) % steps_per_checkpoint == 0 or step + 1 == num_steps:
      state = imagenet_train_utils.sync_batch_stats(state)
      save_checkpoint(state)


  # Wait until computations are done before exiting
  jax.random.normal(jax.random.PRNGKey(0), ()).block_until_ready()


if __name__ == '__main__':
  app.run(main)
