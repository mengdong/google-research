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

"""Generates ConfigDict instances for the Imagenet.

Most users will just create a ConfigDict instance with 'get_config' and then
override its parameters to specialize the configuration.
"""

import ml_collections
from aqt.utils import config_schema_utils

float_ph = config_schema_utils.float_ph
int_ph = config_schema_utils.int_ph
str_ph = config_schema_utils.str_ph
bool_ph = config_schema_utils.bool_ph


def get_base_config(use_auto_acts):
  """Base ConfigDict for resnet, does not yet have fields for individual layers."""
  base_config = config_schema_utils.get_base_config(
      use_auto_acts, fp_quant=False)
  base_config.update({
      "base_learning_rate": float_ph(),
      "momentum": float_ph(),
      "model_hparams": {},
  })

  base_config.dense_layer = config_schema_utils.get_dense_config(base_config)
  # TODO(b/179063860): The input distribution is an intrinsic model
  # property and shouldn't be part of the model configuration. Update
  # the hparam dataclasses to eliminate the input_distribution field and
  # then delete this.
  base_config.dense_layer.quant_act.input_distribution = "positive"
  base_config.conv = config_schema_utils.get_conv_config(base_config)
  base_config.residual = get_residual_config(base_config)
  return base_config


def get_residual_config(
    parent_config):
  """Creates ConfigDict corresponding to imagenet.models.ResidualBlock.HParams."""
  config = ml_collections.ConfigDict()
  config_schema_utils.set_default_reference(
      config,
      parent_config, ["conv_proj", "conv_1", "conv_2", "conv_3"],
      parent_field="conv")
  # TODO(b/179063860): The input distribution is an intrinsic model
  # property and shouldn't be part of the model configuration. Update
  # the hparam dataclasses to eliminate the input_distribution field and
  # then delete this.
  config.conv_proj.quant_act.input_distribution = "positive"
  config.conv_2.quant_act.input_distribution = "positive"
  config.conv_3.quant_act.input_distribution = "positive"

  config.lock()
  return config


def get_config(num_blocks,
               use_auto_acts):
  """Returns a ConfigDict instance for a Imagenet (Resnet50 and Resnet101).

  The ConfigDict is wired up so that changing a field at one level of the
  hierarchy changes the value of that field everywhere downstream in the
  hierarchy. For example, changing the top-level 'prec' parameter
  (eg, config.prec=4) will cause the precision of all layers to change.
  Changing the precision of a specific layer type
  (eg, config.residual_block.conv_1.weight_prec=4) will cause the weight prec
  of all conv_1 layers to change, overriding the value of the global
  config.prec value.

  See config_schema_test.test_schema_matches_expected to see the structure
  of the ConfigDict instance this will return.

  Args:
    num_blocks: Number of residual blocks in the architecture.
    use_auto_acts: Whether to use automatic clipping bounds for activations or
      fixed bounds. Unlike other properties of the configuration which can be
      overridden directly in the ConfigDict instance, this affects the immutable
      schema of the ConfigDict and so has to be specified before the ConfigDict
      is created.

  Returns:
    A ConfigDict instance which parallels the hierarchy of TrainingHParams.
  """
  base_config = get_base_config(use_auto_acts=use_auto_acts)
  model_hparams = base_config.model_hparams
  config_schema_utils.set_default_reference(model_hparams, base_config,
                                            "dense_layer")

  config_schema_utils.set_default_reference(
      model_hparams, base_config, "conv_init", parent_field="conv")
  model_hparams.residual_blocks = [
      config_schema_utils.make_reference(base_config, "residual")
      for _ in range(num_blocks)
  ]
  model_hparams.update({
      # Controls the number of parameters in the model by multiplying the number
      # of conv filters in each layer by this number.
      "filter_multiplier": float_ph(),
  })

  base_config.lock()
  return base_config
