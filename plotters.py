################################################################################
# This file contains helper functions for plotting things.                     #
################################################################################

# imports
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

def plot_true_vs_pred(y_true, y_pred):
    fig, ax = plt.subplots(1,1, figsize=(3,3))
    ax.scatter(y_true, y_pred, c='crimson')

    ax.set_xlabel('True Values')
    ax.set_ylabel('Predictions')
    ax.axis('equal')
    ax.axis('square')

    # ax.plot(y_pred - y_true, marker='o',linestyle='')

    return fig, ax


def plot_with_predictions(model,
                          graph_dataset,
                          Loader,
                          batch_size=32,
                          node=0,
                          model_name=''):
    """ 
        Args:
            model: a tensorflow/keras/spektral model
            graph_dataset: a spektral Dataset object
            node (int): node for which data will be plotted
            model_name (str): (optional) model name to be displayed on the plot
    """
    # set up plot
    fig, axs = plt.subplots(2, 2, figsize=(20, 8))

    title = "time series forecasting"
    if model_name != '':
        title += " for " + model_name

    fig.suptitle(title, size=28)
    axs[0, 0].set_title("X1 (i.e. atmospheric variable) for node {}".format(node),
                  size=20)
    axs[1, 0].set_title("X2 (i.e. oceanic variable) for node {}".format(node),
                  size=20)
    axs[1, 0].set_xlabel('time (days)', size=16)

    for g in graph_dataset:
        axs[0, 0].plot(g.t_X,
                 g.x[node][:graph_dataset.input_steps],
                 label='inputs',
                 c='purple', alpha=0.75)
        axs[1, 0].plot(g.t_X,
                 g.x[node][graph_dataset.input_steps:],
                 label='inputs',
                 c='purple', alpha=0.75)
        axs[0, 0].scatter(g.t_Y,
                    g.y[node][:graph_dataset.output_steps],
                    label='labels',
                    s=30,
                    c='green')

    # generate predictions
    loader = Loader(dataset=graph_dataset, batch_size=batch_size)
    predictions = model.predict(loader.load(), steps=loader.steps_per_epoch)

    # plot predictions
    for i in range(len(graph_dataset)):
        g = graph_dataset[i]
        pred = predictions[i][node]
        axs[0, 0].scatter(g.t_Y, pred, label='prediction', s=30, c='red')
        axs[0, 1].scatter(g.y[node][:graph_dataset.output_steps], pred, c='blue')

    # ax0.set_xlim(graph_dataset[0].t_X[0], graph_dataset[-1].t_Y[-1])
    # ax1.set_xlim(graph_dataset[0].t_X[0], graph_dataset[-1].t_Y[-1])
    # p1 = max(max(predicted_value), max(true_value))
    # p2 = min(min(predicted_value), min(true_value))
    # plt.plot([p1, p2], [p1, p2], 'b-')
    axs[0, 1].plot()
    axs[0, 1].set_xlabel('True Values')
    axs[0, 1].set_ylabel('Predictions')
    axs[0, 1].axis('equal')
    axs[0, 1].axis('square')
    

    return fig, axs