"""Script to run the baselines."""
import argparse
import importlib
import numpy as np
import os
import sys
import random
import tensorflow as tf
import metrics.writer as metrics_writer

from baseline_constants import MAIN_PARAMS, MODEL_PARAMS
from client import Client
from server import Server
from model import ServerModel

from utils.args import parse_args
from utils.model_utils import read_data

from ray import tune
from ray.tune.schedulers import ASHAScheduler

DATA_PATH=['/global', 'cfs', 'cdirs', 'mp156', 'rayleaf_dataset']

args = parse_args() 

def train_federated(config):
    random.seed(1 + args.seed)
    np.random.seed(12 + args.seed)
    tf.compat.v1.set_random_seed(123 + args.seed)

    model_path = '%s/%s.py' % (args.dataset, args.model)
    if not os.path.exists(model_path):
        print('Please specify a valid dataset and a valid model.')
    model_path = '%s.%s' % (args.dataset, args.model)
    
    print('############################## %s ##############################' % model_path)

    mod = importlib.import_module(model_path)
    ClientModel = getattr(mod, 'ClientModel') 

    tup = MAIN_PARAMS[args.dataset][args.t]
    num_rounds = args.num_rounds if args.num_rounds != -1 else tup[0]
    eval_every = args.eval_every if args.eval_every != -1 else tup[1]
    clients_per_round = args.clients_per_round if args.clients_per_round != -1 else tup[2]

    config["seed"] = args.seed

    # Suppress tf warnings; changed from WARN to ERROR 
    tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)

    # Create 2 models
    # model_params = MODEL_PARAMS[model_path]
    # if args.lr != -1:
    #    model_params_list = list(model_params)
    #    model_params_list[0] = args.lr
    #    model_params = tuple(model_params_list)

    # Create client model, and share params with server model
    tf.compat.v1.reset_default_graph()
    client_model = ClientModel(**config)

    # Create server
    server = Server(client_model)

    # Create clients
    clients = setup_clients(args.dataset, client_model, args.use_val_set)
    client_ids, client_groups, client_num_samples = server.get_clients_info(clients)
    print('Clients in Total: %d' % len(clients))

    # Initial status
    print('--- Random Initialization ---')
    stat_writer_fn = get_stat_writer_function(client_ids, client_groups, client_num_samples, args)
    sys_writer_fn = get_sys_writer_function(args)
    print_stats(0, server, clients, client_num_samples, args, stat_writer_fn, args.use_val_set, sys_metrics=None)

    # Simulate training
    for i in range(num_rounds):
        print('--- Round %d of %d: Training %d Clients ---' % (i + 1, num_rounds, clients_per_round))

        # Select clients to train this round
        server.select_clients(i, online(clients), num_clients=clients_per_round)
        c_ids, c_groups, c_num_samples = server.get_clients_info(server.selected_clients)

        # Simulate server model training on selected clients' data
        sys_metrics = server.train_model(num_epochs=args.num_epochs, batch_size=args.batch_size, minibatch=args.minibatch)
        sys_writer_fn(i + 1, c_ids, sys_metrics, c_groups, c_num_samples)
        
        # Update server model
        server.update_model()

        # Test model
        if (i + 1) % eval_every == 0 or (i + 1) == num_rounds:
            print_stats(i + 1, server, clients, client_num_samples, args, stat_writer_fn, args.use_val_set, sys_metrics)

    # TODO: Checkpointing disabled, should really enable... 
    # Save server model
    # ckpt_path = os.path.join('checkpoints', args.dataset)
    # if not os.path.exists(ckpt_path):
    #     os.makedirs(ckpt_path)
    # save_path = server.save_model(os.path.join(ckpt_path, '{}_{}.ckpt'.format(args.model, args.metrics_name)))
    # print('Model saved in path: %s' % save_path)

    # Close models
    server.close_model()

def online(clients):
    """We assume all users are always online."""
    return clients


def create_clients(users, groups, train_data, test_data, model):
    if len(groups) == 0:
        groups = [[] for _ in users]
    clients = [Client(u, g, train_data[u], test_data[u], model) for u, g in zip(users, groups)]
    return clients


def setup_clients(dataset, model=None, use_val_set=False):
    """Instantiates clients based on given train and test data directories.

    Return:
        all_clients: list of Client objects.
    """
    eval_set = 'test' if not use_val_set else 'val'
    train_dir = DATA_PATH + ['data', dataset, 'train']
    test_dir = DATA_PATH + ['data', dataset, eval_set]
    train_data_dir = os.path.join(*train_dir)
    test_data_dir = os.path.join(*test_dir)
    users, groups, train_data, test_data = read_data(train_data_dir, test_data_dir)

    clients = create_clients(users, groups, train_data, test_data, model)

    return clients


def get_stat_writer_function(ids, groups, num_samples, args):

    def writer_fn(num_round, metrics, partition):
        metrics_writer.print_metrics(
            num_round, ids, metrics, groups, num_samples, partition, args.metrics_dir, '{}_{}'.format(args.metrics_name, 'stat'), 0)

    return writer_fn


def get_sys_writer_function(args):

    def writer_fn(num_round, ids, metrics, groups, num_samples):
        metrics_writer.print_metrics(
            num_round, ids, metrics, groups, num_samples, 'train', args.metrics_dir, '{}_{}'.format(args.metrics_name, 'sys'), 1)

    return writer_fn


def print_stats(
    num_round, server, clients, num_samples, args, writer, use_val_set, sys_metrics):
    
    train_stat_metrics = server.test_model(clients, set_to_use='train')
    print_metrics(train_stat_metrics, num_samples, prefix='train_', raytunelog=False) # For now, just log the test performance
    writer(num_round, train_stat_metrics, 'train')

    eval_set = 'test' if not use_val_set else 'val'
    test_stat_metrics = server.test_model(clients, set_to_use=eval_set)

    # Another hack: Add the number of bytes communicated in the epoch (a sys metric) to the stats printout here.
    # This is very unsafe, it only works because this time, we're not changing the model size at all. 
    if sys_metrics is None:
        epoch_bytes = 0
    else:
        epoch_bytes = next(iter(sys_metrics.values()))['bytes_read'] 
 
    print_metrics(test_stat_metrics, num_samples, prefix='{}_'.format(eval_set), raytunelog=True, epoch_bytes=epoch_bytes)
    writer(num_round, test_stat_metrics, eval_set)


def print_metrics(metrics, weights, prefix='', raytunelog=False, epoch_bytes=None):
    """Prints weighted averages of the given metrics.

    Args:
        metrics: dict with client ids as keys. Each entry is a dict
            with the metrics of that client.
        weights: dict with client ids as keys. Each entry is the weight
            for that client.
    """
    ordered_weights = [weights[c] for c in sorted(weights)]
    metric_names = metrics_writer.get_metrics_names(metrics)
    to_ret = None
    for metric in metric_names:
        ordered_metric = [metrics[c][metric] for c in sorted(metrics)]
        average_metric=np.average(ordered_metric, weights=ordered_weights)
        ten_percentile=np.percentile(ordered_metric, 10)
        fifty_percentile=np.percentile(ordered_metric, 50)
        ninety_percentile=np.percentile(ordered_metric, 90)
        print('%s: %g, 10th percentile: %g, 50th percentile: %g, 90th percentile %g' \
              % (prefix + metric,
                 average_metric,
                 ten_percentile,
                 fifty_percentile,
                 ninety_percentile))
        if raytunelog and metric=='accuracy':          # This is another hack; should fix this. 
            tune.report(accuracy=average_metric, ten_percentile=ten_percentile, \
                 fifty_percentile=fifty_percentile, ninety_percentile=ninety_percentile, epoch_bytes=epoch_bytes) 

# TODO: This restrics RAYLEAF to only FEMNIST, just for the purpose of testing the model. 
if __name__ == '__main__':
    sched = ASHAScheduler(metric='accuracy')
    config={
        "lr": 0.06, # tune.uniform(0.005, 0.09),
        "num_classes": 62,
        "dense_rank": 8,
        "factorization": tune.grid_search(
	[
            [[56, 56], [32, 64]],         # 2D factorization 
	    [[14, 14, 16], [16, 16, 8]],  # 3D factorization
            [[7, 8, 8, 7], [4, 8, 8, 8]]  # 4D factorization
	]
       )
    }
    analysis = tune.run(train_federated,
                        name='tt_cnn',
                        # scheduler=sched,
                        stop={"accuracy": 0.99,
                              "training_iteration": 3000},
                        num_samples=1,
                        config=config) 

