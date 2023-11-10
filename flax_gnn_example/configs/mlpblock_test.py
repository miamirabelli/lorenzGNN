# Copyright 2023 The Flax Authors.
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

"""Defines the default hyperparameters and training configuration.

Uses a GraphNetwork model (https://arxiv.org/abs/1806.01261).
"""

import ml_collections


def get_config():
  """Get the hyperparameter configuration for the GraphNetwork model."""
  config = ml_collections.ConfigDict()

  # Optimizer.
  config.optimizer = 'adam'
  config.learning_rate = 1e-3

  # Training hyperparameters.
  config.batch_size = 3
  config.epochs = 4
  config.num_train_steps = 100_000 # TODO is this different from epochs?
  config.log_every_steps = 2
  config.eval_every_steps = 1
  config.checkpoint_every_steps = 2
  config.add_virtual_node = True # TODO what is this? 
  config.add_undirected_edges = True # TODO what is this? 
  config.add_self_loops = True # TODO what is this? 

  # GNN hyperparameters.
  config.model = 'MLPBlock'
#   config.message_passing_steps = 5
#   config.latent_size = 256
  config.dropout_rate = 0.1
#   config.num_mlp_layers = 1
#   config.num_classes = 128
#   config.use_edge_model = True
  config.skip_connections = True
  config.layer_norm = True
  return config
