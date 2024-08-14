from typing import Tuple, Union

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from nas_bench_graph import link_list

from torch_geometric.data import Data
from torch_geometric.nn import GCNConv, GATConv, GINConv, SAGEConv, ChebConv, ARMAConv, GraphConv
from torch_geometric.nn import MessagePassing

from for_other_dataset_exp.llm4gnas.utils.utils import compute_metric


class TaskHead(nn.Module):
    def __init__(self, config, **kwargs):
        self.config = config
        super().__init__()

    def forward(self, x: torch.Tensor):
        # return Tensor
        pass


class GNNBase(MessagePassing):
    def __init__(self, desc, config, **kwargs):
        super().__init__()
        self.config = config
        self.desc = desc
        self.readout = TaskHead(config)

    def forward(self, data: Data):
        # return Tensor
        raise NotImplementedError

    def loss(self, data: Data, out=None):
        # return loss: Tensor
        raise NotImplementedError

    def metric(self, data: Union[Data, Tuple], out=None):
        # return dict: {metric 1: 0.01}
        raise NotImplementedError


# loss 没有用到 val
class NodeClassificationHead(TaskHead):
    def forward(self, x: torch.Tensor):
        return x

    def task_loss(self, data: Data, out):
        loss = F.cross_entropy(out[data.train_mask], data.y[data.train_mask])
        return loss

    def task_metric(self, data: Data, out):
        # return dict: {metric 1: 0.01}
        with torch.no_grad():
            pred = out.argmax(dim=-1)

            accs = []
            for mask in [data.train_mask, data.val_mask, data.test_mask]:
                accs.append(int((pred[mask] == data.y[mask]).sum()) / int(mask.sum()))
            #print({"train acc": accs[0], "val acc": accs[1], "test acc": accs[2]})
        return {"train acc": accs[0], "val acc": accs[1], "test acc": accs[2]}


class GraphClassification(TaskHead):
    def forward(self, x: torch.Tensor):
        return x

    def task_loss(self, data: Data, out):
        loss = F.cross_entropy(out, data.y)
        return loss

    def task_metric(self, dataloader: Tuple, gnn: GNNBase):
        train_loader, val_loader, test_loader = dataloader
        metric = {"train acc": .0, "val acc": .0, "test acc": .0}
        loader_dict = {
            "train": train_loader,
            "val": val_loader,
            "test": test_loader
        }

        for loader_name, loader in loader_dict.items():
            labels = []
            predictions = []
            for batch in loader:
                batch = batch.to(self.config.device)
                label = batch.y
                prediction = gnn(batch)
                labels.append(label)
                predictions.append(prediction)
            predictions = torch.cat(predictions, dim=0)
            labels = torch.cat(labels, dim=0)
            metric[f"{loader_name} acc"] = compute_metric(predictions, labels)
        # print(metric)
        return metric


class CO_problem(TaskHead):
    def forward(self, x: torch.Tensor):
        return x

    def task_loss(self, prob, Q):
        probs_ = torch.unsqueeze(prob, 1)
        loss = (probs_.T @ Q @ probs_).squeeze()
        return loss

    def task_metric(self, maxcut, total_edges):
        result = maxcut / total_edges
        return result


class GCNOnly(GNNBase):
    def __init__(self, desc, config):
        super().__init__(desc, config)
        in_dim, hid_dim, out_dim = self.config.in_dim, self.config.hid_dim, self.config.out_dim
        n_layer = len(desc)

        self.gnns = nn.ModuleList()

        for i in range(n_layer):
            in_ = in_dim if i == 0 else hid_dim
            out_ = out_dim if i == n_layer - 1 else hid_dim
            self.gnns.append(GCNConv(in_, out_))
        if config.task_name == 'NodeClassification':
            self.readout = NodeClassificationHead(config)
        elif config.task_name == 'CO_problem':
            self.readout = CO_problem(config)
        elif config.task_name == 'GraphClassification':
            self.readout = GraphClassification(config)

    def forward(self, data: Data):
        # return Tensor
        x, edge_index = data.x, data.edge_index
        for gnn in self.gnns:
            x = F.dropout(x, p=0.5, training=self.training)
            x = gnn(x, edge_index).relu()
        x = self.readout(x)
        return x

    def loss(self, data: Data = None, out=None, prob=None, Q=None):
        if self.config.task_name == 'CO_problem':
            if prob is not None and Q is not None:
                loss_ = self.readout.task_loss(prob, Q)
            else:
                raise ValueError("prob or Q is None")
        else:
            if data is None or out is None:
                raise ValueError("data or out is None")
            else:
                loss_ = self.readout.task_loss(data, out)
        return loss_

    def metric(self, data: Data = None, out=None, maxcut=None, total_edges=None):
        if self.config.task_name == 'CO_problem':
            if maxcut is not None and total_edges is not None:
                metric_ = self.readout.task_metric(maxcut, total_edges)
            else:
                raise ValueError("for 'CO_problem', values for 'maxcut' and 'total_edges' are required.")
        else:
            if data is None or out is None:
                raise ValueError("data or out is None")
            else:
                metric_ = self.readout.task_metric(data, out)
        return metric_


def get_case(op, in_, out_):
    cheb_order = 2
    case = {
        'gcn': GCNConv(in_, out_),
        'gat': GATConv(in_, out_),
        'sage': SAGEConv(in_, out_),
        'gin': GINConv(nn.Sequential(nn.Linear(in_, out_), nn.ReLU())),
        'cheb': ChebConv(in_, out_, cheb_order),
        'arma': ARMAConv(in_, out_),
        'graph': GraphConv(in_, out_),
        'fc': nn.Linear(in_, out_),
        'skip': nn.Linear(in_, out_),
    }
    return case.get(op)


def best_link(dataset_name):
    link_num = None
    if dataset_name == 'pubmed':
        link_num = 2
    elif dataset_name == 'arxiv':
        link_num = 8
    elif dataset_name == 'cora':
        link_num = 7
    elif dataset_name == 'citeseer':
        link_num = 6
    return link_list[link_num]




class GraphNAS(GNNBase):
    def __init__(self, actions, config, drop_out=0.6, multi_label=False, batch_normal=True, residual=True,
                 state_num=5):
        super().__init__(actions, config)
        self.config = config
        self.multi_label = multi_label
        self.num_feat = self.config.num_feat
        self.num_label = self.config.num_label
        self.dropout = drop_out
        self.residual = residual
        # check structure of GNN
        self.layer_nums = self.evalate_actions(actions, state_num)
        if config.task_name == 'NodeClassification':
            self.readout = NodeClassificationHead(config)
        elif config.task_name == 'CO_problem':
            self.readout = CO_problem(config)
        elif config.task_name == 'GraphClassification':
            self.readout = GraphClassification(config)
        # layer module
        self.build_model(actions, batch_normal, drop_out, self.num_feat, self.num_label, state_num)

    def build_model(self, actions, batch_normal, drop_out, num_feat, num_label, state_num):
        self.layers = torch.nn.ModuleList()
        self.gates = torch.nn.ModuleList()
        self.prediction = None
        self.build_hidden_layers(actions, batch_normal, drop_out, self.layer_nums, num_feat, num_label, state_num)

    def evalate_actions(self, actions, state_num):
        state_length = len(actions)
        if state_length % state_num != 0:
            raise RuntimeError("Wrong Input: unmatchable input")
        layer_nums = state_length // state_num
        if self.evaluate_structure(actions, layer_nums, state_num=state_num):
            pass
        else:
            raise RuntimeError("wrong structure")
        return layer_nums

    def evaluate_structure(self, actions, layer_nums, state_num=6):
        hidden_units_list = []
        out_channels_list = []
        for i in range(layer_nums):
            head_num = actions[i * state_num + 3]
            out_channels = actions[i * state_num + 4]
            hidden_units_list.append(head_num * out_channels)
            out_channels_list.append(out_channels)
        return out_channels_list[-1] == self.num_label

    def build_hidden_layers(self, actions, batch_normal, drop_out, layer_nums, num_feat, num_label, state_num=5):

        # build hidden layer
        for i in range(layer_nums):

            # extract operator types from action
            attention_type = actions[i * state_num + 0]
            aggregator_type = actions[i * state_num + 1]
            act = actions[i * state_num + 2]
            head_num = actions[i * state_num + 3]
            out_channels = actions[i * state_num + 4]

            # compute input
            if i == 0:
                in_channels = num_feat
            else:
                in_channels = out_channels * head_num

            # Multi-head used in GAT.
            # "concat" is True, concat output of each head;
            # "concat" is False, get average of each head output;
            concat = True
            if i == layer_nums - 1:
                concat = False  # The last layer get average
            else:
                pass

            if i == 0:
                residual = False and self.residual  # special setting of dgl
            else:
                residual = True and self.residual
            self.layers.append(
                NASLayer(attention_type, aggregator_type, act, head_num, in_channels, out_channels, dropout=drop_out,
                         concat=concat, residual=residual, batch_normal=batch_normal))

    def forward(self, feat, g):
        output = feat
        for i, layer in enumerate(self.layers):
            output = layer(output, g)

        return output


    def __repr__(self):
        result_lines = ""
        for each in self.layers:
            result_lines += str(each)
        return result_lines

        # map GNN's parameters into dict

    def get_param_dict(self, old_param=None, update_all=True):
        if old_param is None:
            result = {}
        else:
            result = old_param
        for i in range(self.layer_nums):
            key = "layer_%d" % i
            new_param = self.layers[i].get_param_dict()
            if key in result:
                new_param = NASLayer.merge_param(result[key], new_param, update_all)
                result[key] = new_param
            else:
                result[key] = new_param
        return result

        # load parameters from parameter dict

    def load_param(self, param):
        if param is None:
            return
        for i in range(self.layer_nums):
            self.layers[i].load_param(param["layer_%d" % i])

    def loss(self, data: Data = None, out=None, prob=None, Q=None):
        if self.config.task_name == 'CO_problem':
            if prob is not None and Q is not None:
                loss_ = self.readout.task_loss(prob, Q)
            else:
                raise ValueError("prob or Q is None")
        else:
            if data is None or out is None:
                raise ValueError("data or out is None")
            else:
                loss_ = self.readout.task_loss(data, out)
        return loss_

    def metric(self, data: Data = None, out=None, maxcut=None, total_edges=None):
        if self.config.task_name == 'CO_problem':
            if maxcut is not None and total_edges is not None:
                metric_ = self.readout.task_metric(maxcut, total_edges)
            else:
                raise ValueError("for 'CO_problem', values for 'maxcut' and 'total_edges' are required.")
        else:
            if data is None or out is None:
                raise ValueError("data or out is None")
            else:
                metric_ = self.readout.task_metric(data, out)
        return metric_


def gat_message(edges):
    if 'norm' in edges.src:
        msg = edges.src['ft'] * edges.src['norm']
        return {'ft': edges.src['ft'], 'a2': edges.src['a2'], 'a1': edges.src['a1'], 'norm': msg}
    return {'ft': edges.src['ft'], 'a2': edges.src['a2'], 'a1': edges.src['a1']}


class NASLayer(nn.Module):
    def __init__(self, attention_type, aggregator_type, act, head_num, in_channels, out_channels=8, concat=True,
                 dropout=0.6, pooling_dim=128, residual=False, batch_normal=True):
        '''
        build one layer of GNN
        :param attention_type:
        :param aggregator_type:
        :param act: Activation function
        :param head_num: head num, in another word repeat time of current ops
        :param in_channels: input dimension
        :param out_channels: output dimension
        :param concat: concat output. get average when concat is False
        :param dropout: dropput for current layer
        :param pooling_dim: hidden layer dimension; set for pooling aggregator
        :param residual: whether current layer has  skip-connection
        :param batch_normal: whether current layer need batch_normal
        '''
        super(NASLayer, self).__init__()
        # print("NASLayer", in_channels, concat, residual)
        self.attention_type = attention_type
        self.aggregator_type = aggregator_type
        self.act = NASLayer.act_map(act)
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.num_heads = int(head_num)
        self.concat = concat
        self.dropout = dropout
        self.attention_dim = 1
        self.pooling_dim = pooling_dim

        self.batch_normal = batch_normal

        if attention_type in ['cos', 'generalized_linear']:
            self.attention_dim = 64
        self.bn = nn.BatchNorm1d(self.in_channels, momentum=0.5)
        self.prp = nn.ModuleList()
        self.red = nn.ModuleList()
        self.fnl = nn.ModuleList()
        self.agg = nn.ModuleList()
        for hid in range(self.num_heads):
            # due to multi-head, the in_dim = num_hidden * num_heads
            self.prp.append(AttentionPrepare(in_channels, out_channels, self.attention_dim,
                                             dropout))  # return {'h': h, 'ft': ft, 'a1': a1, 'a2': a2}
            agg = NASLayer.aggregator_map(aggregator_type, out_channels, pooling_dim)
            self.agg.append(agg)
            self.red.append(NASLayer.attention_map(attention_type, dropout, agg, self.attention_dim))
            self.fnl.append(GATFinalize(hid, in_channels,
                                        out_channels, NASLayer.act_map(act), residual))

    @staticmethod
    def aggregator_map(aggregator_type, in_dim, pooling_dim):
        if aggregator_type == "sum":
            return SumAggregator()
        elif aggregator_type == "mean":
            return MeanPoolingAggregator(in_dim, pooling_dim)
        elif aggregator_type == "max":
            return MaxPoolingAggregator(in_dim, pooling_dim)
        elif aggregator_type == "mlp":
            return MLPAggregator(in_dim, pooling_dim)
        elif aggregator_type == "lstm":
            return LSTMAggregator(in_dim, pooling_dim)
        elif aggregator_type == "gru":
            return GRUAggregator(in_dim, pooling_dim)
        else:
            raise Exception("wrong aggregator type", aggregator_type)

    @staticmethod
    def attention_map(attention_type, attn_drop, aggregator, attention_dim):
        if attention_type == "gat":
            return GATReduce(attn_drop, aggregator)
        elif attention_type == "cos":
            return CosReduce(attn_drop, aggregator)
        elif attention_type in ["none", "const"]:
            return ConstReduce(attn_drop, aggregator)
        elif attention_type == "gat_sym":
            return GatSymmetryReduce(attn_drop, aggregator)
        elif attention_type == "linear":
            return LinearReduce(attn_drop, aggregator)
        elif attention_type == "bilinear":
            return CosReduce(attn_drop, aggregator)
        elif attention_type == "generalized_linear":
            return GeneralizedLinearReduce(attn_drop, attention_dim, aggregator)
        elif attention_type == "gcn":
            return GCNReduce(attn_drop, aggregator)
        else:
            raise Exception("wrong attention type")

    @staticmethod
    def act_map(act):
        if act == "linear":
            return lambda x: x
        elif act == "elu":
            return F.elu
        elif act == "sigmoid":
            return torch.sigmoid
        elif act == "tanh":
            return torch.tanh
        elif act == "relu":
            return torch.nn.functional.relu
        elif act == "relu6":
            return torch.nn.functional.relu6
        elif act == "softplus":
            return torch.nn.functional.softplus
        elif act == "leaky_relu":
            return torch.nn.functional.leaky_relu
        else:
            raise Exception("wrong activate function")

    def get_param_dict(self):
        params = {}

        key = "%d_%d_%d_%s" % (self.in_channels, self.out_channels, self.num_heads, self.attention_type)
        prp_key = key + "_" + str(self.attention_dim) + "_prp"
        agg_key = key + "_" + str(self.pooling_dim) + "_" + self.aggregator_type
        fnl_key = key + "_fnl"
        bn_key = "%d_bn" % self.in_channels
        params[prp_key] = self.prp.state_dict()
        params[agg_key] = self.agg.state_dict()
        # params[key+"_"+self.attention_type] = self.red.state_dict()
        params[fnl_key] = self.fnl.state_dict()
        params[bn_key] = self.bn.state_dict()
        return params

    def load_param(self, param):
        key = "%d_%d_%d_%s" % (self.in_channels, self.out_channels, self.num_heads, self.attention_type)
        prp_key = key + "_" + str(self.attention_dim) + "_prp"
        agg_key = key + "_" + str(self.pooling_dim) + "_" + self.aggregator_type
        fnl_key = key + "_fnl"
        bn_key = "%d_bn" % self.in_channels
        if prp_key in param:
            self.prp.load_state_dict(param[prp_key])

        # red_key = key+"_"+self.attention_type
        if agg_key in param:
            self.agg.load_state_dict(param[agg_key])
            for i in range(self.num_heads):
                self.red[i].aggregator = self.agg[i]

        if fnl_key in param:
            self.fnl.load_state_dict(param[fnl_key])

        if bn_key in param:
            self.bn.load_state_dict(param[bn_key])

    @staticmethod
    def merge_param(old_param, new_param, update_all):
        for key in new_param:
            if update_all or key not in old_param:
                old_param[key] = new_param[key]
        return old_param

    def forward(self, features, g):
        if self.batch_normal:
            last = self.bn(features)
        else:
            last = features

        for hid in range(self.num_heads):
            i = hid
            # prepare
            g.ndata.update(self.prp[i](last))
            # message passing
            g.update_all(gat_message, self.red[i], self.fnl[i])
        # merge all the heads
        if not self.concat:
            """
            output = g.pop_n_repr('head0')
            for hid in range(1, self.num_heads):
                output = torch.add(output, g.pop_n_repr('head%d' % hid))
            output = output / self.num_heads
            """
            output = []
            for hid in range(self.num_heads):
                output.append(g.ndata.pop('head%d' % hid))
            output = torch.stack(output, dim=1)
            if not self.concat:
                output = output.mean(dim=1)
        else:
            """
             output = torch.cat(
                [g.pop_n_repr('head%d' % hid) for hid in range(self.num_heads)], dim=1)
            """
            output = torch.cat([g.ndata.pop('head%d' % hid) for hid in range(self.num_heads)], dim=1)

        del last
        return output


class AttentionPrepare(nn.Module):
    '''
        Attention Prepare Layer
    '''

    def __init__(self, input_dim, hidden_dim, attention_dim, drop):
        super(AttentionPrepare, self).__init__()
        self.fc = nn.Linear(input_dim, hidden_dim, bias=False)
        if drop:
            self.drop = nn.Dropout(drop)
        else:
            self.drop = 0
        self.attn_l = nn.Linear(hidden_dim, attention_dim, bias=False)
        self.attn_r = nn.Linear(hidden_dim, attention_dim, bias=False)
        nn.init.xavier_normal_(self.fc.weight.data, gain=1.414)
        nn.init.xavier_normal_(self.attn_l.weight.data, gain=1.414)
        nn.init.xavier_normal_(self.attn_r.weight.data, gain=1.414)

    def forward(self, feats):
        h = feats
        if self.drop:
            h = self.drop(h)
        ft = self.fc(h)
        a1 = self.attn_l(ft)
        a2 = self.attn_r(ft)
        return {'h': h, 'ft': ft, 'a1': a1, 'a2': a2}


class GATReduce(nn.Module):
    def __init__(self, attn_drop, aggregator=None):
        super(GATReduce, self).__init__()
        if attn_drop:
            self.attn_drop = nn.Dropout(p=attn_drop)
        else:
            self.attn_drop = 0
        self.aggregator = aggregator

    def apply_agg(self, neighbor):
        if self.aggregator:
            return self.aggregator(neighbor)
        else:
            return torch.sum(neighbor, dim=1)

    def forward(self, nodes):
        a1 = torch.unsqueeze(nodes.data['a1'], 1)  # shape (B, 1, 1)
        a2 = nodes.mailbox['a2']  # shape (B, deg, 1)
        ft = nodes.mailbox['ft']  # shape (B, deg, D)
        # attention
        a = a1 + a2  # shape (B, deg, 1)
        a = a.sum(-1, keepdim=True)  # Just in case the dimension is not zero
        e = F.softmax(F.leaky_relu(a), dim=1)
        if self.attn_drop:
            e = self.attn_drop(e)
        return {'accum': self.apply_agg(e * ft)}  # shape (B, D)


class ConstReduce(GATReduce):
    '''
        Attention coefficient is 1
    '''

    def __init__(self, attn_drop, aggregator=None):
        super(ConstReduce, self).__init__(attn_drop, aggregator)

    def forward(self, nodes):
        ft = nodes.mailbox['ft']  # shape (B, deg, D)
        # attention
        if self.attn_drop:
            ft = self.attn_drop(ft)
        return {'accum': self.apply_agg(1 * ft)}  # shape (B, D)


class GCNReduce(GATReduce):
    '''
        Attention coefficient is 1
    '''

    def __init__(self, attn_drop, aggregator=None):
        super(GCNReduce, self).__init__(attn_drop, aggregator)

    def forward(self, nodes):
        if 'norm' not in nodes.data:
            raise Exception("Wrong Data, has no norm")
        self_norm = nodes.data['norm']
        self_norm = self_norm.unsqueeze(1)
        results = nodes.mailbox['norm'] * self_norm
        return {'accum': self.apply_agg(results)}  # shape (B, D)


class LinearReduce(GATReduce):
    '''
        equal to neighbor's self-attention
    '''

    def __init__(self, attn_drop, aggregator=None):
        super(LinearReduce, self).__init__(attn_drop, aggregator)

    def forward(self, nodes):
        ft = nodes.mailbox['ft']  # shape (B, deg, D)
        a2 = nodes.mailbox['a2']
        a2 = a2.sum(-1, keepdim=True)  # shape (B, deg, D)
        # attention
        e = F.softmax(torch.tanh(a2), dim=1)
        if self.attn_drop:
            e = self.attn_drop(e)
        return {'accum': self.apply_agg(e * ft)}  # shape (B, D)


class GatSymmetryReduce(GATReduce):
    '''
        gat Symmetry version ( Symmetry cannot be guaranteed after softmax)
    '''

    def __init__(self, attn_drop, aggregator=None):
        super(GatSymmetryReduce, self).__init__(attn_drop, aggregator)

    def forward(self, nodes):
        a1 = torch.unsqueeze(nodes.data['a1'], 1)  # shape (B, 1, 1)
        b1 = torch.unsqueeze(nodes.data['a2'], 1)  # shape (B, 1, 1)
        a2 = nodes.mailbox['a2']  # shape (B, deg, 1)
        b2 = nodes.mailbox['a1']  # shape (B, deg, 1)
        ft = nodes.mailbox['ft']  # shape (B, deg, D)
        # attention
        a = a1 + a2  # shape (B, deg, 1)
        b = b1 + b2  # different attention_weight
        a = a + b
        a = a.sum(-1, keepdim=True)  # Just in case the dimension is not zero
        e = F.softmax(F.leaky_relu(a + b), dim=1)
        if self.attn_drop:
            e = self.attn_drop(e)
        return {'accum': self.apply_agg(e * ft)}  # shape (B, D)


class CosReduce(GATReduce):
    '''
        used in Gaan
    '''

    def __init__(self, attn_drop, aggregator=None):
        super(CosReduce, self).__init__(attn_drop, aggregator)

    def forward(self, nodes):
        a1 = torch.unsqueeze(nodes.data['a1'], 1)  # shape (B, 1, 1)
        a2 = nodes.mailbox['a2']  # shape (B, deg, 1)
        ft = nodes.mailbox['ft']  # shape (B, deg, D)
        # attention
        a = a1 * a2
        a = a.sum(-1, keepdim=True)  # shape (B, deg, 1)
        e = F.softmax(F.leaky_relu(a), dim=1)
        if self.attn_drop:
            e = self.attn_drop(e)
        return {'accum': self.apply_agg(e * ft)}  # shape (B, D)


class GeneralizedLinearReduce(GATReduce):
    '''
        used in GeniePath
    '''

    def __init__(self, attn_drop, hidden_dim, aggregator=None):
        super(GeneralizedLinearReduce, self).__init__(attn_drop, aggregator)
        self.generalized_linear = nn.Linear(hidden_dim, 1, bias=False)
        if attn_drop:
            self.attn_drop = nn.Dropout(p=attn_drop)
        else:
            self.attn_drop = 0

    def forward(self, nodes):
        a1 = torch.unsqueeze(nodes.data['a1'], 1)  # shape (B, 1, 1)
        a2 = nodes.mailbox['a2']  # shape (B, deg, 1)
        ft = nodes.mailbox['ft']  # shape (B, deg, D)
        # attention
        a = a1 + a2
        a = torch.tanh(a)
        a = self.generalized_linear(a)
        e = F.softmax(a, dim=1)
        if self.attn_drop:
            e = self.attn_drop(e)
        return {'accum': self.apply_agg(e * ft)}  # shape (B, D)


class SumAggregator(nn.Module):

    def __init__(self):
        super(SumAggregator, self).__init__()

    def forward(self, neighbor):
        return torch.sum(neighbor, dim=1)


class MaxPoolingAggregator(SumAggregator):

    def __init__(self, input_dim, pooling_dim=512, num_fc=1, act=F.leaky_relu_):
        super(MaxPoolingAggregator, self).__init__()
        out_dim = input_dim
        self.fc = nn.ModuleList()
        self.act = act
        if num_fc > 0:
            for i in range(num_fc - 1):
                self.fc.append(nn.Linear(out_dim, pooling_dim))
                out_dim = pooling_dim
            self.fc.append(nn.Linear(out_dim, input_dim))

    def forward(self, ft):
        for layer in self.fc:
            ft = self.act(layer(ft))

        return torch.max(ft, dim=1)[0]


class MeanPoolingAggregator(MaxPoolingAggregator):

    def __init__(self, input_dim, pooling_dim=512, num_fc=1, act=F.leaky_relu_):
        super(MeanPoolingAggregator, self).__init__(input_dim, pooling_dim, num_fc, act)

    def forward(self, ft):
        for layer in self.fc:
            ft = self.act(layer(ft))

        return torch.mean(ft, dim=1)


class MLPAggregator(MaxPoolingAggregator):

    def __init__(self, input_dim, pooling_dim=512, num_fc=1, act=F.leaky_relu_):
        super(MLPAggregator, self).__init__(input_dim, pooling_dim, num_fc, act)

    def forward(self, ft):
        ft = torch.sum(ft, dim=1)
        for layer in self.fc:
            ft = self.act(layer(ft))
        return ft


class LSTMAggregator(SumAggregator):

    def __init__(self, input_dim, pooling_dim=512):
        super(LSTMAggregator, self).__init__()
        self.lstm = nn.LSTM(input_dim, pooling_dim, batch_first=True, bias=False)
        self.linear = nn.Linear(pooling_dim, input_dim)

    def forward(self, ft):
        torch.transpose(ft, 1, 0)
        hidden = self.lstm(ft)[0]
        return self.linear(torch.squeeze(hidden[-1], dim=0))


class GRUAggregator(SumAggregator):

    def __init__(self, input_dim, pooling_dim=512):
        super(LSTMAggregator, self).__init__()
        self.lstm = nn.GRU(input_dim, pooling_dim, batch_first=True, bias=False)
        self.linear = nn.Linear(pooling_dim, input_dim)

    def forward(self, ft):
        torch.transpose(ft, 1, 0)
        hidden = self.lstm(ft)[0]
        return self.linear(torch.squeeze(hidden[-1], dim=0))


class GATFinalize(nn.Module):
    '''
        concat + fully connected layer
    '''

    def __init__(self, headid, indim, hiddendim, activation, residual):
        super(GATFinalize, self).__init__()
        self.headid = headid
        self.activation = activation
        self.residual = residual
        self.residual_fc = None
        if residual:
            if indim != hiddendim:
                self.residual_fc = nn.Linear(indim, hiddendim, bias=False)
                nn.init.xavier_normal_(self.residual_fc.weight.data, gain=1.414)

    def forward(self, nodes):
        ret = nodes.data['accum']
        if self.residual:
            if self.residual_fc is not None:
                ret = self.residual_fc(nodes.data['h']) + ret
            else:
                ret = nodes.data['h'] + ret
        return {'head%d' % self.headid: self.activation(ret)}
