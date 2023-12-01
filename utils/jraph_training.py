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
from clu import parameter_overview
from clu import periodic_actions
import flax
import flax.core
import flax.linen as nn
from flax.training import train_state
import jax
import jax.numpy as jnp
import jraph
import ml_collections
# import numpy as np
import optax
# import sklearn.metrics
import pdb 

# from . import input_pipeline
from utils.jraph_models import MLPBlock, MLPGraphNetwork
from utils.jraph_data import get_lorenz_graph_tuples, print_graph_fts

def create_model(
    config: ml_collections.ConfigDict, deterministic: bool
) -> nn.Module:
    """Creates a Flax model, as specified by the config."""
    if config.model == 'MLPBlock':
        return MLPBlock(
            dropout_rate=config.dropout_rate,
            skip_connections=config.skip_connections,
            layer_norm=config.layer_norm,
            deterministic=deterministic,
            edge_features=config.edge_features,
            node_features=config.node_features,
            global_features=config.global_features,
        )
    elif config.model == 'MLPGraphNetwork':
        return MLPGraphNetwork(
            n_blocks=config.n_blocks,
            share_params=config.share_params,
            dropout_rate=config.dropout_rate,
            skip_connections=config.skip_connections,
            layer_norm=config.layer_norm,
            deterministic=deterministic,
            edge_features=config.edge_features,
            node_features=config.node_features,
            global_features=config.global_features,
        )

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

def create_dataset(    
    config: ml_collections.ConfigDict,
) -> Dict[str, Dict[str, Iterable[jraph.GraphsTuple]]]:
    dataset = get_lorenz_graph_tuples(
        n_samples=config.n_samples,
        input_steps=config.input_steps,
        output_delay=config.output_delay,
        output_steps=config.output_steps,
        timestep_duration=config.timestep_duration,
        sample_buffer=config.sample_buffer,
        time_resolution=config.time_resolution,
        init_buffer_samples=config.init_buffer_samples,
        train_pct=config.train_pct,
        val_pct=config.val_pct,
        test_pct=config.test_pct,
        K=config.K,
        F=config.F,
        c=config.c,
        b=config.b,
        h=config.h,
        seed=config.seed,
        normalize=config.normalize,
        fully_connected_edges=config.fully_connected_edges)

    return dataset

# define loss functions 
def MSE(targets, preds):
    mse = jnp.mean(jnp.square(preds - targets))
    return mse 


# def unbatch_i(batched_graph, i):
#    """ Retrieve the ith graph in a batched graphtuple. This helper function is jittable and replaced the jraph.unbatch function, which cannot be jitted. """
#    n_graphs = batched_graph.n_edge.shape[0]
# #    assert i < n_graphs # this line is not jittable. :( 

#    node_start_idx = jax.lax.dynamic_slice(batched_graph.n_node, start_indices=(0,), slice_sizes=(i,)).sum()
#    # the i variable here is not jittable. kms 
#    node_end_idx = jax.lax.dynamic_slice(batched_graph.n_node, start_indices=(i,), slice_sizes=(n_graphs-i,)).sum() - n_graphs.n_node[i]
# #    edge_start_idx = batched_graph.n_edge[:i].sum()
# #    edge_end_idx = batched_graph.n_edge[i:].sum() - n_graphs.n_edge[i]
   
#    selected_graph = jraph.GraphsTuple(
#         globals=batched_graph.globals[i],
#         nodes=batched_graph.nodes[node_start_idx:node_end_idx, :],
#         edges=batched_graph.edges[edge_start_idx:edge_end_idx, :],
#         receivers=batched_graph.receivers[edge_start_idx:edge_end_idx],
#         senders=batched_graph.senders[edge_start_idx:edge_end_idx],
#         n_node=jnp.array([batched_graph.n_node[i]]),
#         n_edge=jnp.array([batched_graph.n_edge[i]]),
#    )
   
#    return selected_graph 


def rollout_loss(state: train_state.TrainState, 
                input_window_graphs: Iterable[jraph.GraphsTuple],
                target_window_graphs: Iterable[jraph.GraphsTuple],
                 n_rollout_steps: int,
                 rngs: Optional[Dict[str, jnp.ndarray]],
                 ) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """ Computes average loss for an n-step rollout. 
    
        Also returns predicted nodes.
    """
    # TODO: not urgent, but this could be refactored to call the rollout function
    # TODO: theoretically n_rollout_steps can be eliminated and we just base the rollout on the size of the target_graphs list. however, for now we are passing in n_rollout_steps because i don't know how else we can do the jax jit with argnames 
    assert n_rollout_steps > 0
    assert len(target_window_graphs) == n_rollout_steps, (len(target_window_graphs), n_rollout_steps)

    curr_input_window_graphs = input_window_graphs
    pred_nodes = []
    total_loss = 0
    for i in range(n_rollout_steps):
        pred_graphs_list = state.apply_fn(state.params, curr_input_window_graphs, rngs=rngs) 
        pred_graph = pred_graphs_list[0]

        # retrieve the new input window 
        curr_input_window_graphs = curr_input_window_graphs[1:] + [pred_graph]

        preds = pred_graph.nodes
        targets = target_window_graphs[i].nodes
    
        loss = MSE(targets, preds)

        pred_nodes.append(preds) # Side-effects aren't allowed in JAX-transformed functions, and appending to a list is a side effect ??

        total_loss += loss

    avg_loss = total_loss / n_rollout_steps

    return avg_loss, pred_nodes


def rollout(state: train_state.TrainState, 
                input_window_graphs: Iterable[jraph.GraphsTuple],
                # target_window_graphs: Iterable[jraph.GraphsTuple],
                 n_rollout_steps: int,
                 rngs: Optional[Dict[str, jnp.ndarray]],
                 ) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """ Computes rollout predictions. 
    """
    # TODO: theoretically n_rollout_steps can be eliminated and we just base the rollout on the size of the target_graphs list. however, for now we are passing in n_rollout_steps because i don't know how else we can do the jax jit with argnames 
    assert n_rollout_steps > 0
    # assert len(target_window_graphs) == n_rollout_steps, (len(target_window_graphs), n_rollout_steps)

    curr_input_window_graphs = input_window_graphs
    pred_nodes = []
    # total_loss = 0
    for i in range(n_rollout_steps):
        pred_graphs_list = state.apply_fn(state.params, curr_input_window_graphs, rngs=rngs) 
        pred_graph = pred_graphs_list[0]

        # retrieve the new input window 
        curr_input_window_graphs = curr_input_window_graphs[1:] + [pred_graph]

        preds = pred_graph.nodes
        # targets = target_window_graphs[i].nodes
    
        # loss = MSE(targets, preds)

        pred_nodes.append(preds) # Side-effects aren't allowed in JAX-transformed functions, and appending to a list is a side effect ??

        # total_loss += loss

    # avg_loss = total_loss / n_rollout_steps

    return pred_nodes # list of jnp arrays of size (36, 2)

# TODO this is currently malfunctioning 
# rollout_loss_batched = jax.vmap(rollout_loss, in_axes=[None, 1, 1, None])
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


def train_step_fn(
    state: train_state.TrainState,
    n_rollout_steps: int,
    input_window_graphs: Iterable[jraph.GraphsTuple],
    target_window_graphs: Iterable[jraph.GraphsTuple],
    # TODO: update once batched rollout is fixed
    # batch_input_graphs: Iterable[jraph.GraphsTuple], 
    # batch_target_graphs: Iterable[Iterable[jraph.GraphsTuple]], 
    rngs: Dict[str, jnp.ndarray],
) -> Tuple[train_state.TrainState, metrics.Collection, jnp.ndarray]:
    """ Performs one update step over the current batch of graphs.
    
        Args: 
        state (flax train_state.TrainState): TrainState containing the model's 
            call function, the model's params, and the optimizer 
        input_window_graphs: list of graphs constituting a single window of 
            input data 
        target_window_graphs: list of graphs constituting a single window of 
            target data 
        # batch_input_graphs (list of GraphsTuples): batch (list) of 
        #     GraphsTuples, which each contain a window of input graphs
        # batch_target_graphs (GraphsTuple): batch (list) of GraphsTuples each 
        #     containing a rollout window of the target output states
        #     NOTE: the number of output graphs in this GraphsTuple object 
        #     indicates the number of rollout steps that should be performed
        rngs (dict): rngs where the key of the dict denotes the rng use 
    """
    assert n_rollout_steps > 0
    assert len(target_window_graphs) == n_rollout_steps, (len(target_window_graphs), n_rollout_steps)

    def loss_fn(params, input_window_graphs, target_window_graphs):
        curr_state = state.replace(params=params) # create a new state object so that we can pass the whole thing into the one_step_loss function. we do this so that we can keep track of the original state's apply_fn() and a custom param together (theoretically the param argument in this function doesn't need to be the same as the default state's param)

        # Compute loss.
        loss, pred_nodes = rollout_loss(
           state=curr_state, input_window_graphs=input_window_graphs, 
           target_window_graphs=target_window_graphs, n_rollout_steps=n_rollout_steps, 
           rngs=rngs)
        return loss, pred_nodes
        # TODO trace where rngs is used, this is unclear. dropout? 

    grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
    (loss, pred_nodes), grads = grad_fn(state.params, input_window_graphs, target_window_graphs)
    state = state.apply_gradients(grads=grads) # update params in the state 

    metrics_update = TrainMetrics.single_from_model_output(loss=loss)

    return state, metrics_update, pred_nodes

train_step = jax.jit(train_step_fn, static_argnames=["n_rollout_steps"])


def evaluate_step_fn(
    state: train_state.TrainState,
    n_rollout_steps: int,
    input_window_graphs: Iterable[jraph.GraphsTuple],
    target_window_graphs: Iterable[jraph.GraphsTuple],
) -> Tuple[metrics.Collection, jnp.ndarray]:
    """Computes metrics over a set of graphs."""

    # Get node predictions and loss 

    loss, pred_nodes = rollout_loss(state=state, 
                                    input_window_graphs=input_window_graphs, 
                                    target_window_graphs=target_window_graphs, 
                                    n_rollout_steps=n_rollout_steps, rngs=None) 

    eval_metrics_dict = EvalMetrics.single_from_model_output(loss=loss)

    return eval_metrics_dict, pred_nodes

evaluate_step = jax.jit(evaluate_step_fn, static_argnames=["n_rollout_steps"])

def evaluate_model(
    state: train_state.TrainState,
    n_rollout_steps: int,
    datasets: Dict[str, Dict[str, Iterable[jraph.GraphsTuple]]], 
    # first key = train/test/val, second key = input/target 
    splits: Iterable[str], # e.g. ["val", "test"]
) -> Dict[str, metrics.Collection]:
    """Evaluates the model on metrics over the specified splits."""

    # Loop over each split independently.
    eval_metrics_dict = {}
    for split in splits:
        # splits = e.g. 'val', 'test
        split_metrics = None

        input_data = datasets[split]['inputs']
        target_data = datasets[split]['targets']

        # loop over individual windows in the dataset 
        for (input_window_graphs, target_window_graphs) in zip(
            input_data, target_data):
            split_metrics_update, _ = evaluate_step(
                state=state, 
                n_rollout_steps=n_rollout_steps, 
                input_window_graphs=input_window_graphs, 
                target_window_graphs=target_window_graphs)

            # Update metrics.
            if split_metrics is None:
                split_metrics = split_metrics_update
            else:
                split_metrics = split_metrics.merge(split_metrics_update)
        
        eval_metrics_dict[split] = split_metrics

    return eval_metrics_dict  # pytype: disable=bad-return-type


def add_prefix_to_keys(result: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    """ Adds a prefix to the keys of a dict, returning a new dict.
    
        This is a helper function for logging during training/evaluation.
    """
    return {f'{prefix}_{key}': val for key, val in result.items()}

def train_and_evaluate(
    config: ml_collections.ConfigDict, workdir: str
) -> Tuple[train_state.TrainState, TrainMetrics, EvalMetrics]:
    # Get datsets. 
    logging.info('Obtaining datasets.')
    datasets = create_dataset(config)

    return train_and_evaluate_with_data(
        config=config, workdir=workdir, datasets=datasets)
 

def train_and_evaluate_with_data(
    config: ml_collections.ConfigDict, workdir: str, 
    datasets: Dict[str, Dict[str, Iterable[jraph.GraphsTuple]]]
) -> Tuple[train_state.TrainState, TrainMetrics, EvalMetrics]:
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
    train_set = datasets['train']
    input_data = train_set['inputs']
    target_data = train_set['targets']
    n_rollout_steps = config.output_steps

    # Create and initialize the network.
    logging.info('Initializing network.')
    rng = jax.random.key(0)
    rng, init_rng = jax.random.split(rng)
    sample_input_window = input_data[0]
    init_net = create_model(config, deterministic=True)
    params = jax.jit(init_net.init)(init_rng, sample_input_window)
    parameter_overview.log_parameter_overview(params) # logs to logging.info

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
    initial_step = int(state.step) # state.step is 0-indexed 
    init_epoch = initial_step // len(input_data) # 0-indexed 

    # Create the evaluation state, corresponding to a deterministic model.
    eval_net = create_model(config, deterministic=True)
    eval_state = state.replace(apply_fn=eval_net.apply)

    num_train_steps = config.epochs * len(train_set['inputs'])
    # Hooks called periodically during training.
    report_progress = periodic_actions.ReportProgress(
        num_train_steps=num_train_steps, writer=writer
    )
    profiler = periodic_actions.Profile(num_profile_steps=5, logdir=workdir)
    hooks = [report_progress, profiler]

    # Begin training loop.
    logging.info('Starting training.')
    train_metrics = None
    # for step in range(initial_step, num_train_steps + 1):
        # epoch = step % len(train_set['inputs'])
    
    # note step is 0-indexed 
    step = initial_step
    for epoch in range(init_epoch, config.epochs):

        # iterate over data
        # right now we don't have batching so we just loop over individual windows in the dataset
        for (input_window_graphs, target_window_graphs) in zip(
            input_data, target_data):
            # Split PRNG key, to ensure different 'randomness' for every step.
            rng, dropout_rng = jax.random.split(rng)

            # Perform one step of training.
            with jax.profiler.StepTraceAnnotation('train', step_num=step):
                # graphs = jax.tree_util.tree_map(np.asarray, next(train_iter))
                state, metrics_update, _ = train_step(
                    state=state, 
                    n_rollout_steps=n_rollout_steps, 
                    input_window_graphs=input_window_graphs, 
                    target_window_graphs=target_window_graphs, 
                    rngs={'dropout': dropout_rng}
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

            step += 1

        # epoch is 0-indexed 
        is_last_epoch = (epoch == config.epochs - 1) 

        # Log, if required.
        if epoch % config.log_every_epochs == 0 or is_last_epoch:
            writer.write_scalars(
                step, add_prefix_to_keys(train_metrics.compute(), 'train')
            )
            train_metrics = None

        # Evaluate on validation and test splits, if required.
        if epoch % config.eval_every_epochs == 0 or is_last_epoch:
            eval_state = eval_state.replace(params=state.params)

            splits = ['val', 'test']
            with report_progress.timed('eval'):
                eval_metrics_dict = evaluate_model(
                    state=eval_state, 
                    n_rollout_steps=n_rollout_steps, 
                    datasets=datasets, 
                    splits=splits)
            for split in splits:
                writer.write_scalars(
                    step, add_prefix_to_keys(eval_metrics_dict[split].compute(), split)
                )

        # Checkpoint model, if required.
        if epoch % config.checkpoint_every_epochs == 0 or is_last_epoch:
            with report_progress.timed('checkpoint'):
                ckpt.save(state)
    return state, train_metrics, eval_metrics_dict