import collections

import numpy as np

import scores
import utils
import model
import torch
import tensorflow as tf
from dataloader import SupervisedDataset
from torch.utils.data import DataLoader
import torch.nn.functional as F
import os
import pandas as pd

# Setting the run number and GPU core to use
RUN = 14
CORE = 0

# for running stuff locally (Tensorflow didn't support my cuda version)
tf.config.set_visible_devices([], 'GPU')

# for turning off annoying warnings
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

# Neuromodulation config
LSTM_TYPE = 'default'  # default, Simple_NM

## initial config
N_EPOCHS = 1000
RESULT_PER_EPOCH = 10 * 1
CHECKPOINT_PER_EPOCH = 5
EVAL_STEPS = 400 # Original 400
CHECKPOINT_PATH = f'checkpoints/run{RUN}/'

NH_LSTM = 128
NH_BOTTLENECK = 256

ENV_SIZE = 2.2
BATCH_SIZE = 10  # original 10
TRAINING_STEPS_PER_EPOCH = 1000    # original 1000
GRAD_CLIPPING = 1e-5
SEED = 9101
N_PC = [256]
N_HDC = [12]
BOTTLENECK_DROPOUT = [0.5]
WEIGHT_DECAY = 1e-5
LR = 1e-5 # Original 1e-5
MOMENTUM = 0.9 # original 0.9
TIME = 50
PAUSE_TIME = None
SAVE_LOC = 'experiments/'

TRAIN_DATA_RANGE = [0, 90]
TEST_DATA_RANGE = [90, 100]

# Setting the name and path for ratemaps (scores) and traces
scores_filename = 'rates_'
scores_directory = f'results/eval/run{RUN}/scores/'
base_trace_filename = 'traces_'
trace_directory = f'results/eval/run{RUN}/traces/'

# path = 'data/tmp/'
path = 'data/'
DatasetInfo = collections.namedtuple(
    'DatasetInfo', ['basepath', 'size', 'sequence_length', 'coord_range'])

_DATASETS = dict(
    square_room=DatasetInfo(
        basepath='square_room_100steps_2.2m_1000000',
        size=100,
        sequence_length=100,
        coord_range=((-1.1, 1.1), (-1.1, 1.1))), )

ds_info = _DATASETS['square_room']

feature_map = {
    'init_pos':
        tf.io.FixedLenFeature(shape=[2], dtype=tf.float32),
    'init_hd':
        tf.io.FixedLenFeature(shape=[1], dtype=tf.float32),
    'ego_vel':
        tf.io.FixedLenFeature(
            shape=[ds_info.sequence_length, 3],
            dtype=tf.float32),
    'target_pos':
        tf.io.FixedLenFeature(
            shape=[ds_info.sequence_length, 2],
            dtype=tf.float32),
    'target_hd':
        tf.io.FixedLenFeature(
            shape=[ds_info.sequence_length, 1],
            dtype=tf.float32),
}

data_params = {
    'batch_size': BATCH_SIZE,
    'shuffle': True,
    'num_workers': 2}
test_params = {
    'batch_size': BATCH_SIZE,
    'shuffle': True,
    'num_workers': 2}


# Equivalent of tf.nn.softmax_crossentropy_with_logits
def Loss_Function(logits, labels):
    logits = F.log_softmax(logits, dim=-1)
    return -(labels * logits).sum(dim=-1)


def softmax_crossentropy_with_logits(labels, logits):
    logits = torch.swapaxes(logits, 0, 1)
    # print('labels', labels.size(), labels[(labels!=0)&(labels!=1)].size())
    # print('logits', logits.size())
    # if labels.size()[2] == 12:
    #     print(labels[0,0,:])
    labels_2d = labels.reshape((-1, labels.size()[2]))
    logits_2d = logits.reshape((-1, logits.size()[2]))
    return Loss_Function(logits_2d, labels_2d)


# Press the green button in the gutter to run the script.
if __name__ == '__main__':
    # loading the data
    train_data_dic = utils.load_datadic_from_tfrecords(path, _DATASETS, 'square_room', feature_map, TRAIN_DATA_RANGE)
    test_data_dic = utils.load_datadic_from_tfrecords(path, _DATASETS, 'square_room', feature_map, TEST_DATA_RANGE)

    use_cuda = torch.cuda.is_available()
    device = torch.device(f'cuda:{CORE}' if use_cuda else 'cpu')
    print('Using device:', device)

    # Dataset and Dataloader
    train_dataset = SupervisedDataset(train_data_dic)
    train_dataloader = DataLoader(train_dataset, **data_params)

    test_dataset = SupervisedDataset(test_data_dic)
    test_dataloader = DataLoader(test_dataset, **data_params)

    # Getting place and head direction cell ensembles
    place_cell_ensembles = utils.get_place_cell_ensembles(
        env_size=ENV_SIZE,
        neurons_seed=SEED,
        targets_type='softmax',
        lstm_init_type='softmax',
        n_pc=N_PC,
        pc_scale=[0.01],
        device=device)

    head_direction_ensembles = utils.get_head_direction_ensembles(
        neurons_seed=SEED,
        targets_type='softmax',
        lstm_init_type='softmax',
        n_hdc=N_HDC,
        hdc_concentration=[20.],
        device=device)

    target_ensembles = place_cell_ensembles + head_direction_ensembles

    # Defining the model and getting its parameters
    gridtorchmodel = model.GridTorch(target_ensembles, NH_LSTM, NH_BOTTLENECK,
                                     dropoutrates_bottleneck=BOTTLENECK_DROPOUT,
                                     LSTM_type=LSTM_TYPE).to(device)
    params = gridtorchmodel.parameters()

    # Optimizer
    optimizer = torch.optim.RMSprop(params,
                                    lr=LR,
                                    momentum=MOMENTUM,
                                    alpha=0.9,
                                    eps=1e-10)

    # For storing losses
    epoch_losses = []
    test_losses = []
    epoch_hd_losses = []
    epoch_pc_losses = []

    start_epoch = 1000
    # if trying to resume training from a checkpoint, comment otherwise
    checkpoint = torch.load(CHECKPOINT_PATH + f'model_{start_epoch:04d}.pt')
    # start_epoch = checkpoint['epoch'] + 1
    gridtorchmodel.load_state_dict(checkpoint['model_state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer_state_dict'])

    for epoch in range(start_epoch, 1001):
        # No train mode
        gridtorchmodel.eval()
        step = 1
        losses = []
        hd_losses = []
        pc_losses = []

        activations = []
        posxy = []

        # Evaluating on training data for the specified number of steps
        with torch.no_grad():
            for X, y in train_dataloader:
                # optimizer.zero_grad()

                init_pos, init_hd, ego_vel = X
                target_pos, target_hd = y

                init_pos = init_pos.to(device)
                init_hd = init_hd.to(device)
                ego_vel = torch.swapaxes(ego_vel.to(device), 0, 1)

                target_pos = target_pos.to(device)
                target_hd = target_hd.to(device)

                # Getting initial conditions
                init_conds = utils.encode_initial_conditions(init_pos, init_hd, place_cell_ensembles,
                                                             head_direction_ensembles)

                # Getting ensemble targets
                ensemble_targets = utils.encode_targets(target_pos, target_hd, place_cell_ensembles,
                                                        head_direction_ensembles)

                # Initial Hebbian Trace
                if LSTM_TYPE == 'Simple_NM':
                    hebb = torch.zeros((init_pos.shape[0], NH_LSTM, NH_LSTM), device=device)
                else:
                    hebb = None

                # Running through the model
                outs = gridtorchmodel(ego_vel, init_conds, hebb)

                # Collecting different parts of the output
                logits_hd, logits_pc, bottleneck_acts, rnn_states, rnn_cells = outs

                pc_loss = softmax_crossentropy_with_logits(labels=ensemble_targets[0], logits=logits_pc)
                hd_loss = softmax_crossentropy_with_logits(labels=ensemble_targets[1], logits=logits_hd)

                total_loss = pc_loss + hd_loss
                train_loss = total_loss.mean()

                # weight decay
                train_loss += gridtorchmodel.l2_loss * WEIGHT_DECAY

                # train_loss.backward()

                # Gradient Clipping
                # torch.nn.utils.clip_grad_value_(params, GRAD_CLIPPING)

                # optimizer.step()

                losses.append(train_loss.clone().item())
                hd_losses.append(hd_loss.mean().clone().item())
                pc_losses.append(pc_loss.mean().clone().item())
                # print('Loss:', losses[-1])

                if step >= TRAINING_STEPS_PER_EPOCH:
                    break
                step += 1

        # Logging
        losses_np = np.array(losses)
        epoch_loss_mean = losses_np.mean()
        epoch_loss_std = losses_np.std()
        epoch_losses.append(epoch_loss_mean)
        print(f'Epoch {epoch:4d}:  Loss Mean:{epoch_loss_mean:4.4f}  Loss Std:{epoch_loss_std:4.4f}')

        pc_losses_np = np.array(pc_losses)
        hd_losses_np = np.array(hd_losses)
        pc_losses_mean = pc_losses_np.mean()
        pc_losses_std = pc_losses_np.std()
        epoch_pc_losses.append(pc_losses_mean)
        hd_losses_mean = hd_losses_np.mean()
        hd_losses_std = hd_losses_np.std()
        epoch_hd_losses.append(hd_losses_mean)
        print(f'Mean PC loss:{pc_losses_mean:4.4f}  PC loss std:{pc_losses_std:4.4f}')
        print(f'Mean HD loss:{hd_losses_mean:4.4f}  HD loss std:{hd_losses_std:4.4f}')

        # Evaluation
        gridtorchmodel.eval()
        eval_steps = 1
        losses = []

        activations = []
        target_posxy = []
        pred_posxy = []

        with torch.no_grad():
            for X, y in test_dataloader:
                init_pos, init_hd, ego_vel = X
                target_pos, target_hd = y

                init_pos = init_pos.to(device)
                init_hd = init_hd.to(device)
                ego_vel = torch.swapaxes(ego_vel.to(device), 0, 1)

                target_pos = target_pos.to(device)
                target_hd = target_hd.to(device)

                # Getting initial conditions
                init_conds = utils.encode_initial_conditions(init_pos, init_hd, place_cell_ensembles,
                                                             head_direction_ensembles)
                # Getting ensemble targets
                ensemble_targets = utils.encode_targets(target_pos, target_hd, place_cell_ensembles,
                                                        head_direction_ensembles)
                # Initial Hebbian Trace
                if LSTM_TYPE == 'Simple_NM':
                    hebb = torch.zeros((init_pos.shape[0], NH_LSTM, NH_LSTM), device=device)
                else:
                    hebb = None
                # Running through the model
                outs = gridtorchmodel(ego_vel, init_conds, hebb)

                # Collecting different parts of the output
                logits_hd, logits_pc, bottleneck_acts, rnn_states, rnn_cells = outs

                # Computing test loss
                pc_loss = softmax_crossentropy_with_logits(labels=ensemble_targets[0], logits=logits_pc)
                hd_loss = softmax_crossentropy_with_logits(labels=ensemble_targets[1], logits=logits_hd)

                total_loss = pc_loss + hd_loss
                test_loss = total_loss.mean()

                # weight decay
                test_loss += gridtorchmodel.l2_loss * WEIGHT_DECAY

                losses.append(test_loss.clone().item())

                # accumulating for plotting
                activations.append(torch.swapaxes(bottleneck_acts.detach(), 0, 1))
                target_posxy.append(target_pos.detach())
                pred_posxy.append(torch.swapaxes(logits_pc.detach(), 0, 1))

                if eval_steps >= EVAL_STEPS:
                    break
                eval_steps += 1

            losses_np = np.array(losses)
            test_loss_mean = losses_np.mean()
            test_loss_std = losses_np.std()
            test_losses.append(test_loss_mean)
            print(f'Mean test loss: {test_loss_mean:4.4f}  Test loss std: {test_loss_std:4.4f}')

            activations = torch.cat(activations).cpu().numpy()
            target_posxy = torch.cat(target_posxy).cpu().numpy()
            pred_posxy = torch.cat(pred_posxy).cpu().numpy()

            spatial_err_mean, spatial_err_std  = utils.get_spatial_error(target_posxy, pred_posxy, place_cell_ensembles[0].means.cpu().numpy())
            print(f'Mean spatial err: {spatial_err_mean:4.4f}')
            print(f'Spatial err std: {spatial_err_std:4.4f}')
