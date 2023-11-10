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

"""Library file for executing training and evaluation on ogbg-molpcba."""

import os
from typing import Any, Dict, Iterable, Tuple, Optional

from absl import logging
from clu import checkpoint
from clu import metric_writers
from clu import metrics
# from clu import parameter_overview
from clu import periodic_actions
import flax
import flax.core
import flax.linen as nn
from flax.training import train_state
import jax
import jax.numpy as jnp
import jraph
import ml_collections
import numpy as np
import optax
import sklearn.metrics
import tensorflow as tf

import input_pipeline
import models


def create_model(
    config: ml_collections.ConfigDict, deterministic: bool
) -> nn.Module:
  """Creates a Flax model, as specified by the config."""
  if config.model == 'MLPBlock':
    return models.MLPBlock(
        dropout_rate=config.dropout_rate,
        skip_connections=config.skip_connections,
        layer_norm=config.layer_norm,
        deterministic=config.deterministic)

#   if config.model == 'GraphNet':
#     return models.GraphNet(
#         latent_size=config.latent_size,
#         num_mlp_layers=config.num_mlp_layers,
#         message_passing_steps=config.message_passing_steps,
#         output_globals_size=config.num_classes,
#         dropout_rate=config.dropout_rate,
#         skip_connections=config.skip_connections,
#         layer_norm=config.layer_norm,
#         use_edge_model=config.use_edge_model,
#         deterministic=deterministic,
#     )
#   if config.model == 'GraphConvNet':
#     return models.GraphConvNet(
#         latent_size=config.latent_size,
#         num_mlp_layers=config.num_mlp_layers,
#         message_passing_steps=config.message_passing_steps,
#         output_globals_size=config.num_classes,
#         dropout_rate=config.dropout_rate,
#         skip_connections=config.skip_connections,
#         layer_norm=config.layer_norm,
#         deterministic=deterministic,
#     )
  raise ValueError(f'Unsupported model: {config.model}.')


def create_optimizer(
    config: ml_collections.ConfigDict,
) -> optax.GradientTransformation:
  """Creates an optimizer, as specified by the config."""
  if config.optimizer == 'adam':
    return optax.adam(learning_rate=config.learning_rate)
  if config.optimizer == 'sgd':
    return optax.sgd(
        learning_rate=config.learning_rate, momentum=config.momentum
    )
  raise ValueError(f'Unsupported optimizer: {config.optimizer}.')

# define loss functions 
def MSE(targets, preds):
    mse = jnp.mean(jnp.square(preds - targets))
    return mse 

def one_step_loss(state: train_state.TrainState, 
                  input_graph: jraph.GraphsTuple,
                 target_graph: jraph.GraphsTuple,
                 ) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """ Computes loss for a one-step prediction (no rollout). 
    
        Also returns predicted nodes.
    """
    pred_graph = state.apply_fn(state.params, input_graph) 
    X1_preds = pred_graph.nodes[:, 0] # nodes has shape (36, 2)
    X1_targets = target_graph.nodes[:, 0]

    # MSE loss
    loss = MSE(X1_targets, X1_preds)
    return loss, X1_preds

# TODO update test for this 
def rollout_loss(state: train_state.TrainState, 
                 input_window_graph: jraph.GraphsTuple,
                 target_window_graph: jraph.GraphsTuple, 
                 rngs: Optional[Dict[str, jnp.ndarray]],
                 ) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """ Computes average loss for an n-step rollout. 
    
        Also returns predicted nodes.
    """
    # TODO: theoretically n_steps can be eliminated and we just base the rollout on the size of the target_graphs list 
    target_window_graph_list = jraph.unbatch(target_window_graph)
    n_steps = target_window_graph.n_node.shape[0]

    curr_input_window_graph = input_window_graph
    pred_nodes = []
    total_loss = 0
    for i in range(n_steps):
        pred_graph = state.apply_fn(state.params, curr_input_window_graph, 
                                    rngs=rngs) 
        # we need to unbatch the old input window and rebatch with the new prediction 
        prev_input_window_list = jraph.unbatch(curr_input_window_graph)
        new_input_window_list = prev_input_window_list[1:].append(pred_graph)
        curr_input_window_graph = jraph.batch(new_input_window_list)
        # TODO test this 


        X1_preds = pred_graph.nodes[:, 0]
        target_graph = target_window_graph_list[i]
        X1_targets = target_graph.nodes[:, 0]
        loss = MSE(X1_targets, X1_preds)

        pred_nodes.append(X1_preds) # Side-effects aren't allowed in JAX-transformed functions, and appending to a list is a side effect ??
        total_loss += loss

    avg_loss = total_loss / n_steps

    return avg_loss, pred_nodes

# TODO test this 
rollout_loss_batched = jax.vmap(rollout_loss, in_axes=[None, 1, 1, None])
# batch over the params input_window_graph and target_window_graph but not 
# state or rngs


@flax.struct.dataclass
class EvalMetrics(metrics.Collection):
  loss: metrics.Average.from_output('loss')
  # the loss value is passed in as a named param. it can be either single step 
  # or rollout loss, and is chosen in the training step, so we do not need to 
  # specify it here. 

@flax.struct.dataclass
class TrainMetrics(metrics.Collection):
  loss: metrics.Average.from_output('loss')


# @flax.struct.dataclass
# class EvalMetrics(metrics.Collection):
#   accuracy: metrics.Average.from_fun(predictions_match_labels)
#   loss: metrics.Average.from_output('loss')
#   mean_average_precision: MeanAveragePrecision


# @flax.struct.dataclass
# class TrainMetrics(metrics.Collection):
#   accuracy: metrics.Average.from_fun(predictions_match_labels)
#   loss: metrics.Average.from_output('loss')

# TODO what is this for? (seems like for graph classification which is not what we want)
# def replace_globals(graphs: jraph.GraphsTuple) -> jraph.GraphsTuple:
#   """Replaces the globals attribute with a constant feature for each graph."""
#   return graphs._replace(globals=jnp.ones([graphs.n_node.shape[0], 1]))


# def get_predicted_logits(
#     state: train_state.TrainState,
#     graphs: jraph.GraphsTuple,
#     rngs: Optional[Dict[str, jnp.ndarray]],
# ) -> jnp.ndarray:
#   """Get predicted logits from the network for input graphs."""
#   pred_graphs = state.apply_fn(state.params, graphs, rngs=rngs)
#   logits = pred_graphs.globals
#   return logits


# def get_valid_mask(
#     labels: jnp.ndarray, graphs: jraph.GraphsTuple
# ) -> jnp.ndarray:
#   """Gets the binary mask indicating only valid labels and graphs."""
#   # We have to ignore all NaN values - which indicate labels for which
#   # the current graphs have no label.
#   labels_mask = ~jnp.isnan(labels)

#   # Since we have extra 'dummy' graphs in our batch due to padding, we want
#   # to mask out any loss associated with the dummy graphs.
#   # Since we padded with `pad_with_graphs` we can recover the mask by using
#   # get_graph_padding_mask.
#   graph_mask = jraph.get_graph_padding_mask(graphs)

#   # Combine the mask over labels with the mask over graphs.
#   return labels_mask & graph_mask[:, None]


# TODO check if this can jit properly
@jax.jit
def train_step(
    state: train_state.TrainState,
    batch_input_graphs: Iterable[jraph.GraphsTuple], 
    batch_target_graphs: Iterable[jraph.GraphsTuple], 
    rngs: Dict[str, jnp.ndarray],
) -> Tuple[train_state.TrainState, metrics.Collection]:
    """ Performs one update step over the current batch of graphs.
    
        Args: 
        state (flax train_state.TrainState): TrainState containing 
        batch_input_graphs (list of GraphsTuples): batch (list) of 
            GraphsTuples, which each contain a window of input graphs
        batch_target_graphs (GraphsTuple): batch (list) of GraphsTuples each 
            containing a rollout window of the target output states
            NOTE: the number of output graphs in this GraphsTuple object 
            indicates the number of rollout steps that should be performed
        rngs (dict): rngs where the key of the dict denotes the rng use 
    """

    def loss_fn(params, batch_input_graphs, batch_target_graphs):
        curr_state = state.replace(params=params)
        # create a new state object so that we can pass the whole thing into the one_step_loss function. we do this so that we can keep track of the original state's apply_fn() and a custom param together (theoretically the param argument in this function doesn't need to be the same as the default state's param)

        # Compute loss.
        losses, batch_pred_nodes = rollout_loss_batched(curr_state, batch_input_graphs, batch_target_graphs, rngs=rngs)
        # TODO trace where rngs is used, this is unclear. dropout? 
        return losses, batch_pred_nodes

    grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
    (losses, batch_pred_nodes), grads = grad_fn(state.params, batch_input_graphs, batch_target_graphs)
    state = state.apply_gradients(grads=grads) # update params in the state 

    # TODO how to do batching w this? 
    # the flax example seemed to just input a whole batch of loss... but they also calculated mean loss, which they proceeded to never use 
    metrics_update = TrainMetrics.single_from_model_output(loss=losses)

    return state, metrics_update, batch_pred_nodes


@jax.jit
def evaluate_step(
    state: train_state.TrainState,
    input_graphs: Iterable[jraph.GraphsTuple], # TODO make this iterable or batch? or an iterable of windows (which use graph batching)
    target_graphs: Iterable[jraph.GraphsTuple], # actually this should just be one batched GraphsTuple right? since its an output window 
) -> metrics.Collection:
    """Computes metrics over a set of graphs."""

    # Get node predictions and loss 
    loss, pred_nodes = rollout_loss(state, input_graphs, target_graphs, rngs=None) # TODO why do they set rngs to None here, but use rngs in training? maybe because we dont want dropout during eval?

    eval_metrics = EvalMetrics.single_from_model_output(loss=loss)

    return eval_metrics, pred_nodes


def evaluate_model(
    state: train_state.TrainState,
    datasets: Dict[str, Dict[str, Iterable[jraph.GraphsTuple]]],
    splits: Iterable[str], # e.g. ["train", "val", "test"] ??
) -> Dict[str, metrics.Collection]:
    """Evaluates the model on metrics over the specified splits."""

    # Loop over each split independently.
    eval_metrics = {}
    for split in splits:
        split_metrics = None

        # Loop over graphs.
        for graphs in datasets[split].as_numpy_iterator():
            split_metrics_update = evaluate_step(state, graphs)

            # Update metrics.
            if split_metrics is None:
                    split_metrics = split_metrics_update
            else:
                    split_metrics = split_metrics.merge(split_metrics_update)
        eval_metrics[split] = split_metrics

    return eval_metrics  # pytype: disable=bad-return-type


def add_prefix_to_keys(result: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    """ Adds a prefix to the keys of a dict, returning a new dict.
    
        This is a helper function for logging during training/evaluation.
    """
    return {f'{prefix}_{key}': val for key, val in result.items()}


def train_and_evaluate(
    config: ml_collections.ConfigDict, workdir: str
) -> train_state.TrainState:
  """Execute model training and evaluation loop.

  Args:
    config: Hyperparameter configuration for training and evaluation.
    workdir: Directory where the TensorBoard summaries are written to.

  Returns:
    The train state (which includes the `.params`).
  """
  # We only support single-host training.
  assert jax.process_count() == 1

  # Create writer for logs.
  writer = metric_writers.create_default_writer(workdir)
  writer.write_hparams(config.to_dict())

  # Get datasets, organized by split.
  logging.info('Obtaining datasets.')
  datasets = input_pipeline.get_datasets(
      config.batch_size,
      add_virtual_node=config.add_virtual_node,
      add_undirected_edges=config.add_undirected_edges,
      add_self_loops=config.add_self_loops,
  )
  train_iter = iter(datasets['train'])

  # Create and initialize the network.
  logging.info('Initializing network.')
  rng = jax.random.key(0)
  rng, init_rng = jax.random.split(rng)
  init_graphs = next(datasets['train'].as_numpy_iterator())
  init_graphs = replace_globals(init_graphs)
  init_net = create_model(config, deterministic=True)
  params = jax.jit(init_net.init)(init_rng, init_graphs)
  # commented out bc the package import was failing 
#   parameter_overview.log_parameter_overview(params)

  # Create the optimizer.
  tx = create_optimizer(config)

  # Create the training state.
  net = create_model(config, deterministic=False)
  state = train_state.TrainState.create(
      apply_fn=net.apply, params=params, tx=tx
  )

  # Set up checkpointing of the model.
  # The input pipeline cannot be checkpointed in its current form,
  # due to the use of stateful operations.
  checkpoint_dir = os.path.join(workdir, 'checkpoints')
  ckpt = checkpoint.Checkpoint(checkpoint_dir, max_to_keep=2)
  state = ckpt.restore_or_initialize(state)
  initial_step = int(state.step) + 1

  # Create the evaluation state, corresponding to a deterministic model.
  eval_net = create_model(config, deterministic=True)
  eval_state = state.replace(apply_fn=eval_net.apply)

  # Hooks called periodically during training.
  report_progress = periodic_actions.ReportProgress(
      num_train_steps=config.num_train_steps, writer=writer
  )
  profiler = periodic_actions.Profile(num_profile_steps=5, logdir=workdir)
  hooks = [report_progress, profiler]

  # Begin training loop.
  logging.info('Starting training.')
  train_metrics = None
  for step in range(initial_step, config.num_train_steps + 1):
    # Split PRNG key, to ensure different 'randomness' for every step.
    rng, dropout_rng = jax.random.split(rng)

    # Perform one step of training.
    with jax.profiler.StepTraceAnnotation('train', step_num=step):
      graphs = jax.tree_util.tree_map(np.asarray, next(train_iter))
      state, metrics_update = train_step(
          state, graphs, rngs={'dropout': dropout_rng}
      )

      # Update metrics.
      if train_metrics is None:
        train_metrics = metrics_update
      else:
        train_metrics = train_metrics.merge(metrics_update)

    # Quick indication that training is happening.
    logging.log_first_n(logging.INFO, 'Finished training step %d.', 10, step)
    for hook in hooks:
      hook(step)

    # Log, if required.
    is_last_step = step == config.num_train_steps - 1
    if step % config.log_every_steps == 0 or is_last_step:
      writer.write_scalars(
          step, add_prefix_to_keys(train_metrics.compute(), 'train')
      )
      train_metrics = None

    # Evaluate on validation and test splits, if required.
    if step % config.eval_every_steps == 0 or is_last_step:
      eval_state = eval_state.replace(params=state.params)

      splits = ['validation', 'test']
      with report_progress.timed('eval'):
        eval_metrics = evaluate_model(eval_state, datasets, splits=splits)
      for split in splits:
        writer.write_scalars(
            step, add_prefix_to_keys(eval_metrics[split].compute(), split)
        )

    # Checkpoint model, if required.
    if step % config.checkpoint_every_steps == 0 or is_last_step:
      with report_progress.timed('checkpoint'):
        ckpt.save(state)

  return state
