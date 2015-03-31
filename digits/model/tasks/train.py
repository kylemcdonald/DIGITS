# Copyright (c) 2014-2015, NVIDIA CORPORATION.  All rights reserved.

import time
import os.path
from collections import OrderedDict, namedtuple

from digits import utils
from digits.task import Task

# NOTE: Increment this everytime the picked object changes
PICKLE_VERSION = 2

# Used to store network outputs
NetworkOutput = namedtuple('NetworkOutput', ['kind', 'data'])

class TrainTask(Task):
    """
    Defines required methods for child classes
    """

    def __init__(self, dataset, train_epochs, snapshot_interval, learning_rate, lr_policy, **kwargs):
        """
        Arguments:
        dataset -- a DatasetJob containing the dataset for this model
        train_epochs -- how many epochs of training data to train on
        snapshot_interval -- how many epochs between taking a snapshot
        learning_rate -- the base learning rate
        lr_policy -- a hash of options to be used for the learning rate policy

        Keyword arguments:
        batch_size -- if set, override any network specific batch_size with this value
        val_interval -- how many epochs between validating the model with an epoch of validation data
        pretrained_model -- filename for a model to use for fine-tuning
        crop_size -- crop each image down to a square of this size
        use_mean -- subtract the dataset's mean file
        """
        self.batch_size = kwargs.pop('batch_size', None)
        self.val_interval = kwargs.pop('val_interval', None)
        self.pretrained_model = kwargs.pop('pretrained_model', None)
        self.crop_size = kwargs.pop('crop_size', None)
        self.use_mean = kwargs.pop('use_mean', None)

        super(TrainTask, self).__init__(**kwargs)
        self.pickver_task_train = PICKLE_VERSION

        self.dataset = dataset
        self.train_epochs = train_epochs
        self.snapshot_interval = snapshot_interval
        self.learning_rate = learning_rate
        self.lr_policy = lr_policy

        self.current_epoch = 0
        self.snapshots = []

        # data gets stored as dicts of lists (for graphing)
        self.train_outputs = OrderedDict()
        self.val_outputs = OrderedDict()

    def __getstate__(self):
        state = super(TrainTask, self).__getstate__()
        if 'dataset' in state:
            del state['dataset']
        if 'snapshots' in state:
            del state['snapshots']
        if '_labels' in state:
            del state['_labels']
        return state

    def __setstate__(self, state):
        if state['pickver_task_train'] < 2:
            print 'Upgrading TrainTask to version 2'
            state['train_outputs'] = OrderedDict()
            state['val_outputs'] = OrderedDict()

            tl = state.pop('train_loss_updates', None)
            vl = state.pop('val_loss_updates', None)
            va = state.pop('val_accuracy_updates', None)
            lr = state.pop('lr_updates', None)
            if tl:
                state['train_outputs']['epoch'] = NetworkOutput('Epoch', [u[0] for u in tl])
                state['train_outputs']['loss'] = NetworkOutput('SoftmaxWithLoss', [u[1] for u in tl])
                state['train_outputs']['learning_rate'] = NetworkOutput('LearningRate', [u[1] for u in lr])
            if vl:
                state['val_outputs']['epoch'] = NetworkOutput('Epoch', [u[0] for u in vl])
                state['val_outputs']['loss'] = NetworkOutput('SoftmaxWithLoss', [u[1] for u in vl])
                if va:
                    state['val_outputs']['accuracy'] = NetworkOutput('Accuracy', [u[1] for u in va])
        state['pickver_task_train'] = PICKLE_VERSION

        super(TrainTask, self).__setstate__(state)

        self.snapshots = []
        self.detect_snapshots()
        self.dataset = None

    def send_progress_update(self, epoch):
        """
        Sends socketio message about the current progress
        """
        from digits.webapp import socketio

        if self.current_epoch == epoch:
            return

        self.current_epoch = epoch
        self.progress = epoch/self.train_epochs

        socketio.emit('task update',
                {
                    'task': self.html_id(),
                    'update': 'progress',
                    'percentage': int(round(100*self.progress)),
                    'eta': utils.time_filters.print_time_diff(self.est_done()),
                    },
                namespace='/jobs',
                room=self.job_id,
                )

    def save_train_output(self, *args):
        """
        Save output to self.train_outputs
        """
        from digits.webapp import socketio

        if not self.save_output(self.train_outputs, *args):
            return

        if self.last_train_update and (time.time() - self.last_train_update) < 5:
            return
        self.last_train_update = time.time()

        # loss graph data
        data = self.combined_graph_data()
        if data:
            socketio.emit('task update',
                    {
                        'task': self.html_id(),
                        'update': 'combined_graph',
                        'data': data,
                        },
                    namespace='/jobs',
                    room=self.job_id,
                    )

        # lr graph data
        data = self.lr_graph_data()
        if data:
            socketio.emit('task update',
                    {
                        'task': self.html_id(),
                        'update': 'lr_graph',
                        'data': data,
                        },
                    namespace='/jobs',
                    room=self.job_id,
                    )

    def save_val_output(self, *args):
        """
        Save output to self.val_outputs
        """
        from digits.webapp import socketio
        if not self.save_output(self.val_outputs, *args):
            return

        # loss graph data
        data = self.combined_graph_data()
        if data:
            socketio.emit('task update',
                    {
                        'task': self.html_id(),
                        'update': 'combined_graph',
                        'data': data,
                        },
                    namespace='/jobs',
                    room=self.job_id,
                    )

    def save_output(self, d, name, kind, value):
        """
        Save output to self.train_outputs or self.val_outputs
        Returns true if all outputs for this epoch have been added

        Arguments:
        d -- the dictionary where the output should be stored
        name -- name of the output (e.g. "accuracy")
        kind -- the type of outputs (e.g. "Accuracy")
        value -- value for this output (e.g. 0.95)
        """
        # don't let them be unicode
        name = str(name)
        kind = str(kind)

        # update d['epoch']
        if 'epoch' not in d:
            d['epoch'] = NetworkOutput('Epoch', [self.current_epoch])
        elif d['epoch'].data[-1] != self.current_epoch:
            d['epoch'].data.append(self.current_epoch)

        if name not in d:
            d[name] = NetworkOutput(kind, [])
        epoch_len = len(d['epoch'].data)
        name_len = len(d[name].data)

        # save to back of d[name]
        if name_len > epoch_len:
            raise Exception('Received a new output without being told the new epoch')
        elif name_len == epoch_len:
            # already exists
            if isinstance(d[name].data[-1], list):
                d[name].data[-1].append(value)
            else:
                d[name].data[-1] = [d[name].data[-1], value]
        elif name_len == epoch_len - 1:
            # expected case
            d[name].data.append(value)
        else:
            # we might have missed one
            d[name].data += [None] * (epoch_len - name_len - 1) + [value]

        for key in d:
            if key not in ['epoch', 'learning_rate']:
                if len(d[key].data) != epoch_len:
                    return False
        return True

    def detect_snapshots(self):
        """
        Populate self.snapshots with snapshots that exist on disk
        Returns True if at least one usable snapshot is found
        """
        return False

    def snapshot_list(self):
        """
        Returns an array of arrays for creating an HTML select field
        """
        return [[s[1], 'Epoch #%s' % s[1]] for s in reversed(self.snapshots)]

    def est_next_snapshot(self):
        """
        Returns the estimated time in seconds until the next snapshot is taken
        """
        return None

    def can_view_weights(self):
        """
        Returns True if this Task can visualize the weights of each layer for a given model
        """
        raise NotImplementedError()

    def view_weights(self, model_epoch=None, layers=None):
        """
        View the weights for a specific model and layer[s]
        """
        return None

    def can_infer_one(self):
        """
        Returns True if this Task can run inference on one input
        """
        raise NotImplementedError()

    def can_view_activations(self):
        """
        Returns True if this Task can visualize the activations of a model after inference
        """
        raise NotImplementedError()

    def infer_one(self, data, model_epoch=None, layers=None):
        """
        Run inference on one input
        """
        return None

    def can_infer_many(self):
        """
        Returns True if this Task can run inference on many inputs
        """
        raise NotImplementedError()

    def infer_many(self, data, model_epoch=None):
        """
        Run inference on many inputs
        """
        return None

    def get_labels(self):
        """
        Read labels from labels_file and return them in a list
        """
        # The labels might be set already
        if hasattr(self, '_labels') and self._labels and len(self._labels) > 0:
            return self._labels

        assert hasattr(self.dataset, 'labels_file'), 'labels_file not set'
        assert self.dataset.labels_file, 'labels_file not set'
        assert os.path.exists(self.dataset.path(self.dataset.labels_file)), 'labels_file does not exist'

        labels = []
        with open(self.dataset.path(self.dataset.labels_file)) as infile:
            for line in infile:
                label = line.strip()
                if label:
                    labels.append(label)

        assert len(labels) > 0, 'no labels in labels_file'

        self._labels = labels
        return self._labels

    def lr_graph_data(self):
        """
        Returns learning rate data formatted for a C3.js graph
        """
        if not self.train_outputs or 'epoch' not in self.train_outputs or 'learning_rate' not in self.train_outputs:
            return None

        # return 100-200 values or fewer
        stride = max(len(self.train_outputs['epoch'].data)/100,1)
        e = ['epoch'] + self.train_outputs['epoch'].data[::stride]
        lr = ['lr'] + self.train_outputs['learning_rate'].data[::stride]

        return {
                'columns': [e, lr],
                'xs': {
                    'lr': 'epoch'
                    },
                'names': {
                    'lr': 'Learning Rate'
                    },
                }

    def loss_graph_data(self):
        """
        Returns loss data formatted for a C3.js graph
        """
        data = {
                'columns': [],
                'xs': {},
                'names': {},
                }

        if self.train_outputs and 'epoch' in self.train_outputs:
            added_column = False
            stride = max(len(self.train_outputs['epoch'].data)/100,1)
            for name, output in self.train_outputs.iteritems():
                if name not in ['epoch', 'learning_rate']:
                    if 'loss' in output.kind.lower():
                        col_id = '%s-train' % name
                        data['columns'].append([col_id] + output.data[::stride])
                        data['xs'][col_id] = 'train_epochs'
                        data['names'][col_id] = '%s (train)' % name
                        added_column = True
            if added_column:
                data['columns'].append(['train_epochs'] + self.train_outputs['epoch'].data[::stride])

        if self.val_outputs and 'epoch' in self.val_outputs:
            added_column = False
            stride = max(len(self.val_outputs['epoch'].data)/100,1)
            for name, output in self.val_outputs.iteritems():
                if name not in ['epoch']:
                    if 'loss' in output.kind.lower():
                        col_id = '%s-val' % name
                        data['columns'].append([col_id] + output.data[::stride])
                        data['xs'][col_id] = 'val_epochs'
                        data['names'][col_id] = '%s (val)' % name
                        added_column = True
            if added_column:
                data['columns'].append(['val_epochs'] + self.val_outputs['epoch'].data[::stride])

        if not len(data['columns']):
            return None
        else:
            return data

    def accuracy_graph_data(self):
        """
        Returns accuracy data formatted for a C3.js graph
        """
        data = {
                'columns': [],
                'xs': {},
                'names': {},
                }

        if self.train_outputs and 'epoch' in self.train_outputs:
            added_column = False
            stride = max(len(self.train_outputs['epoch'].data)/100,1)
            for name, output in self.train_outputs.iteritems():
                if name not in ['epoch', 'learning_rate']:
                    if output.kind == 'Accuracy':
                        col_id = '%s-train' % name
                        data['columns'].append([col_id] + output.data[::stride])
                        data['xs'][col_id] = 'train_epochs'
                        data['names'][col_id] = '%s (train)' % name
                        added_column = True
            if added_column:
                data['columns'].append(['train_epochs'] + self.train_outputs['epoch'].data[::stride])

        if self.val_outputs and 'epoch' in self.val_outputs:
            added_column = False
            stride = max(len(self.val_outputs['epoch'].data)/100,1)
            for name, output in self.val_outputs.iteritems():
                if name not in ['epoch']:
                    if output.kind == 'Accuracy':
                        col_id = '%s-val' % name
                        data['columns'].append([col_id] + output.data[::stride])
                        data['xs'][col_id] = 'val_epochs'
                        data['names'][col_id] = '%s (val)' % name
                        added_column = True
            if added_column:
                data['columns'].append(['val_epochs'] + self.val_outputs['epoch'].data[::stride])

        if not len(data['columns']):
            return None
        else:
            return data

    def combined_graph_data(self):
        """
        Returns all train/val outputs in data for one C3.js graph
        """
        data = {
                'columns': [],
                'xs': {},
                'axes': {},
                'names': {},
                }

        if self.train_outputs and 'epoch' in self.train_outputs:
            added_column = False
            stride = max(len(self.train_outputs['epoch'].data)/100,1)
            for name, output in self.train_outputs.iteritems():
                if name not in ['epoch', 'learning_rate']:
                    col_id = '%s-train' % name
                    data['columns'].append([col_id] + output.data[::stride])
                    data['xs'][col_id] = 'train_epochs'
                    data['names'][col_id] = '%s (train)' % name
                    if 'accuracy' in output.kind.lower():
                        data['axes'][col_id] = 'y2'
                    added_column = True
            if added_column:
                data['columns'].append(['train_epochs'] + self.train_outputs['epoch'].data[::stride])

        if self.val_outputs and 'epoch' in self.val_outputs:
            added_column = False
            stride = max(len(self.val_outputs['epoch'].data)/100,1)
            for name, output in self.val_outputs.iteritems():
                if name not in ['epoch']:
                    col_id = '%s-val' % name
                    data['columns'].append([col_id] + output.data[::stride])
                    data['xs'][col_id] = 'val_epochs'
                    data['names'][col_id] = '%s (val)' % name
                    if 'accuracy' in output.kind.lower():
                        data['axes'][col_id] = 'y2'
                    added_column = True
            if added_column:
                data['columns'].append(['val_epochs'] + self.val_outputs['epoch'].data[::stride])

        if not len(data['columns']):
            return None
        else:
            return data

