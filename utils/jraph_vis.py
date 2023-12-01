import jraph
import jax
import jax.numpy as jnp
import networkx as nx
import haiku as hk
from utils.jraph_data import convert_jraph_to_networkx_graph
from utils.jraph_training import rollout, rollout_loss, create_dataset, create_model, create_optimizer
from clu import parameter_overview
from clu import checkpoint
from flax.training import train_state
import matplotlib.pyplot as plt
import numpy as np
import os

from typing import Any, Callable, Dict, List, Optional, Tuple, Iterable
import logging 
import pdb 

# graph visualizations
def draw_jraph_graph_structure(jraph_graph: jraph.GraphsTuple) -> None:
    nx_graph = convert_jraph_to_networkx_graph(jraph_graph)

    pos = nx.circular_layout(nx_graph)
    X1_attr = [nx_graph.nodes[i]['node_feature'][0] for i in nx_graph.nodes]

    nx.draw_networkx(nx_graph,
                     pos=pos,
                     cmap=plt.cm.RdBu,
                     node_color=X1_attr,
                     vmin=-8,
                     vmax=8,
                     with_labels=True,
                     node_size=100,
                     font_size=6,
                     font_color='black')

    # get colorbar
    ax = plt.gca()
    PCM = ax.get_children()[0]  # this index may vary
    plt.colorbar(PCM, ax=ax)
    plt.title('X1 data for nodes')


# time series visualizations
def plot_time_series_for_node(graph_tuple_dict, node):
    fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(20, 8))
    fig.suptitle("sampled time series after reshaping", size=28)
    ax0.set_title("X1 (i.e. atmospheric variable) for node {}".format(node),
                  size=20)
    ax1.set_title("X2 (i.e. oceanic variable) for node {}".format(node),
                  size=20)
    plt.xlabel('time (days)', size=16)

    # plot train data
    ax0.plot(jnp.array([g.nodes[node][0] for g in graph_tuple_dict['train']]),
             label='train')
    ax1.plot(jnp.array([g.nodes[node][1] for g in graph_tuple_dict['train']]),
             label='train')

    # plot val data
    ax0.plot(range(
        len(graph_tuple_dict['train']),
        len(graph_tuple_dict['train']) + len(graph_tuple_dict['val'])),
             jnp.array([g.nodes[node][0] for g in graph_tuple_dict['val']]),
             label='val')
    ax1.plot(range(
        len(graph_tuple_dict['train']),
        len(graph_tuple_dict['train']) + len(graph_tuple_dict['val'])),
             jnp.array([g.nodes[node][1] for g in graph_tuple_dict['val']]),
             label='val')

    ax0.legend()
    ax1.legend()


def plot_rollout_for_node(data_dict_list,
                          timestep_duration,
                          n_rollout_steps,
                          node,
                          title=''):

    fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(20, 8))
    if title == '':
        fig.suptitle(f"rollout for node {node}", size=28)
    else:
        fig.suptitle(title, size=28)
    ax0.set_title("X1 (i.e. atmospheric variable) for node {}".format(node),
                  size=20)
    ax1.set_title("X2 (i.e. oceanic variable) for node {}".format(node),
                  size=20)
    plt.xlabel('time (days)', size=16)

    inputs_x1 = [
        data_dict['input_graph'].nodes[node][0] for data_dict in data_dict_list
    ]
    inputs_x2 = [
        data_dict['input_graph'].nodes[node][1] for data_dict in data_dict_list
    ]

    targets_x1 = jnp.array(
        np.ravel([[
            data_dict['target'][i].nodes[node][0]
            for i in range(n_rollout_steps)
        ] for data_dict in data_dict_list]))
    targets_x2 = jnp.array(
        np.ravel([[
            data_dict['target'][i].nodes[node][1]
            for i in range(n_rollout_steps)
        ] for data_dict in data_dict_list]))

    targets_t = jnp.array(
        np.ravel([[(t + i * timestep_duration)
                   for i in range(1, n_rollout_steps + 1)]
                  for t in range(len(data_dict_list))]))

    # plot inputs
    ax0.plot(inputs_x1, alpha=0.4, label='input', c='blue')
    ax1.plot(inputs_x2, alpha=0.4, label='input', c='blue')
    # plot rollout targets
    ax0.scatter(targets_t, targets_x1, alpha=0.4, label='targets', c='green')
    ax1.scatter(targets_t, targets_x2, alpha=0.4, label='targets', c='green')

    ax0.legend()
    ax1.legend()


def plot_predictions(
        config,
        workdir, # for loading checkpoints 
        plot_ith_rollout_step, # 0 indexed 
        # dataset,
        # preds,
        # timestep_duration,
        # n_rollout_steps,
        #  total_steps,
        node, # 0-indexed 
        plot_all=True, # if false, only plot the first 100 
        title=''):
    assert plot_ith_rollout_step < config.output_steps
    checkpoint_dir = os.path.join(workdir, 'checkpoints')
    assert os.path.exists(checkpoint_dir)

    # samples must be overlapping and consecutive for this plot to really be interpretable the way it is meant 
    assert (
        config.input_steps + config.output_delay + config.output_steps + config.sample_buffer == 1
        )

    # Get datasets, organized by split.
    logging.info('Obtaining datasets.')
    datasets = create_dataset(config)
    train_set = datasets['train']
    input_data = train_set['inputs']
    target_data = train_set['targets']
    # n_rollout_steps = config.output_steps

    # Create the evaluation state, corresponding to a deterministic model.
    logging.info('Initializing network.')
    rng = jax.random.key(0)
    rng, init_rng = jax.random.split(rng)
    sample_input_window = input_data[0]
    eval_net = create_model(config, deterministic=True)
    params = jax.jit(eval_net.init)(init_rng, sample_input_window)
    parameter_overview.log_parameter_overview(params) # logs to logging.info

    # Create the optimizer and state.
    # (we don't actually need the optimizer for evaluation, we just need it to create the state)
    tx = create_optimizer(config)
    state = train_state.TrainState.create(
        apply_fn=eval_net.apply, params=params, tx=tx
    )

    # load the checkpoint state
    ckpt = checkpoint.Checkpoint(checkpoint_dir)
    state = ckpt.restore(state) # restore latest checkpoint 

    # get the predictions from the model for the ith step of the rollout and for the specified node
    node_preds = []
    node_targets = []

    # loop over individual windows in the dataset 
    plot_count = len(input_data) if plot_all else 100
    for i, (input_window_graphs, target_window_graphs) in enumerate(zip(
        input_data, target_data)):
        if i >= plot_count:
            break
        # print('target_window_graphs[0].nodes', target_window_graphs[0].nodes)
        # print('target_window_graphs[1].nodes', target_window_graphs[1].nodes)
        # pdb.set_trace()
        pred_nodes_list = rollout(state=state,
                                  input_window_graphs=input_window_graphs,
                                  n_rollout_steps=config.output_steps,
                                #   n_rollout_steps=plot_ith_rollout_step+1, # +1 since plot_ith_rollout_step is 0-indexed
                                  rngs=None) # deterministic during eval
        
        # get the last array of predictions, which will correspond to the ith rollout step that we care about 
        ith_rollout_pred = pred_nodes_list[plot_ith_rollout_step]
        # print('type(ith_rollout_pred)', type(ith_rollout_pred))
        # print('(ith_rollout_pred)', (ith_rollout_pred.shape))
        node_pred = ith_rollout_pred[node, :] # jnp array with shape (1, 2)
        # print('node_pred.shape', node_pred.shape)
        # print('node_pred', node_pred)
        node_preds.append(node_pred)

        # also grab the target nodes while we're in this loop 
        ith_rollout_target = target_window_graphs[plot_ith_rollout_step].nodes
        # print('type(ith_rollout_target)', type(ith_rollout_target))
        # print('ith_rollout_target.shape', ith_rollout_target.shape)
        # pdb.set_trace()
        node_target = ith_rollout_target[node, :] # jnp array with shape (1, 2)
        # print('node_target.shape', node_target.shape)
        node_targets.append(node_target)

    node_preds = np.vstack(node_preds)
    node_targets = np.vstack(node_targets)

    # print('type(node_preds)', type(node_preds))
    # print((node_preds))
    # print('type(node_targets)', type(node_targets))
    # print((node_targets))

    fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(20, 8))
    if title == '':
        fig.suptitle(f"predictions for node {node}", size=28)
    else:
        fig.suptitle(title, size=28)
    ax0.set_title("X1 (i.e. atmospheric variable) for node {}".format(node),
                  size=20)
    ax1.set_title("X2 (i.e. oceanic variable) for node {}".format(node),
                  size=20)
    plt.xlabel('time (days)', size=16)

    # inputs_x1 = [
    #     window[0].nodes[node][0] for window in dataset['train']['inputs']
    # ] # take only the first graph in every window
    # inputs_x2 = [
    #     window[0].nodes[node][1] for window in dataset['train']['inputs']
    # ] # take only the first graph in every window

    

    # preds_x1 = [pred[node][0] for pred in preds]
    # preds_x2 = [pred[node][1] for pred in preds]

    # targets_x1 = jnp.array(
    #     np.ravel([[
    #         data_dict['target'][i].nodes[node][0]
    #         for i in range(n_rollout_steps)
    #     ] for data_dict in data_dict_list]))
    # targets_x2 = jnp.array(
    #     np.ravel([[
    #         data_dict['target'][i].nodes[node][1]
    #         for i in range(n_rollout_steps)
    #     ] for data_dict in data_dict_list]))

    # targets_t = jnp.array(
    #     np.ravel([[(t + i * timestep_duration)
    #                for i in range(1, n_rollout_steps + 1)]
    #               for t in range(len(data_dict_list))]))

    # TODO reconstruct timesteps

    # TODO do we even care about plotting inputs? 
    # # plot inputs
    # ax0.plot(node_preds[:, 0], alpha=0.8, label='input', c='blue')
    # ax1.plot(node_preds[:, 1], alpha=0.8, label='input', c='blue')

    # plot rollout targets
    ax0.plot(
        # range(len(node_targets)), # TODO replace with actual timesteps 
        node_targets[:, 0],
        # s=5,
        alpha=0.8,
        label='targets',
        c='blue')
    ax1.plot(
        # range(len(node_targets)), # TODO replace with actual timesteps 
        node_targets[:, 1], 
        alpha=0.8, 
        label='targets', 
        c='blue')

    # plot predictions
    ax0.plot(
        # range(len(node_preds)), # TODO replace with actual timesteps 
        node_preds[:, 0],
        # s=5,
        alpha=0.8,
        label='preds',
        c='orange')
    ax1.plot(
        # range(len(node_preds)), # TODO replace with actual timesteps 
        node_preds[:, 1],
        # s=5,
        alpha=0.8,
        label='preds',
        c='orange')

    ax0.legend()
    ax1.legend()

def plot_predictions_old(
        data_dict_list,
        preds,
        timestep_duration,
        n_rollout_steps,
        #  total_steps,
        node,
        title=''):

    fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(20, 8))
    if title == '':
        fig.suptitle(f"predictions for node {node}", size=28)
    else:
        fig.suptitle(title, size=28)
    ax0.set_title("X1 (i.e. atmospheric variable) for node {}".format(node),
                  size=20)
    ax1.set_title("X2 (i.e. oceanic variable) for node {}".format(node),
                  size=20)
    plt.xlabel('time (days)', size=16)

    inputs_x1 = [
        data_dict['input_graph'].nodes[node][0] for data_dict in data_dict_list
    ]
    inputs_x2 = [
        data_dict['input_graph'].nodes[node][1] for data_dict in data_dict_list
    ]

    preds_x1 = [pred[node][0] for pred in preds]
    preds_x2 = [pred[node][1] for pred in preds]

    targets_x1 = jnp.array(
        np.ravel([[
            data_dict['target'][i].nodes[node][0]
            for i in range(n_rollout_steps)
        ] for data_dict in data_dict_list]))
    targets_x2 = jnp.array(
        np.ravel([[
            data_dict['target'][i].nodes[node][1]
            for i in range(n_rollout_steps)
        ] for data_dict in data_dict_list]))

    targets_t = jnp.array(
        np.ravel([[(t + i * timestep_duration)
                   for i in range(1, n_rollout_steps + 1)]
                  for t in range(len(data_dict_list))]))

    # plot inputs
    ax0.plot(inputs_x1, alpha=0.8, label='input', c='blue')
    ax1.plot(inputs_x2, alpha=0.8, label='input', c='blue')

    # plot rollout targets
    ax0.scatter(
        targets_t,
        targets_x1,
        # s=5,
        alpha=0.8,
        label='targets',
        c='green')
    ax1.scatter(targets_t, targets_x2, alpha=0.8, label='targets', c='green')

    # plot predictions
    ax0.scatter(
        range(timestep_duration, timestep_duration + len(data_dict_list)),
        preds_x1,
        # s=5,
        alpha=0.8,
        label='preds',
        c='orange')
    ax1.scatter(
        range(timestep_duration, timestep_duration + len(data_dict_list)),
        preds_x2,
        # s=5,
        alpha=0.8,
        label='preds',
        c='orange')

    ax0.legend()
    ax1.legend()