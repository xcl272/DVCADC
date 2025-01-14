import math

import torch
from torch import nn
import torch.nn.functional as F

from torch_geometric.nn import GATConv, GraphConv
from torch_geometric.nn.pool.topk_pool import filter_adj, topk


class CrossAttentionLayer(nn.Module):
    def __init__(self, embed_dim):
        super().__init__()
        self.embed_dim = embed_dim
        self.query_proj = nn.Linear(embed_dim, embed_dim)
        self.key_proj = nn.Linear(embed_dim, embed_dim)
        self.value_proj = nn.Linear(embed_dim, embed_dim)
        self.out_proj = nn.Linear(embed_dim, embed_dim)

    def forward(self, queries, keys, values):
        # Project the inputs
        queries = self.query_proj(queries)
        keys = self.key_proj(keys)
        values = self.value_proj(values)

        # Compute attention scores
        attention_scores = torch.bmm(queries, keys.transpose(1, 2)) / self.embed_dim ** 0.5
        attention_weights = F.softmax(attention_scores, dim=-1)

        # Compute the output
        attended_values = torch.bmm(attention_weights, values)
        output = self.out_proj(attended_values)

        return output


class MLPLayer(nn.Module):
    def __init__(self, input_dim, embed_dim):
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, 512),  # 可以调整这些层的大小
            nn.ReLU(),
            nn.Linear(512, embed_dim)  # 输出嵌入的维度
        )
        self.decoder = nn.Sequential(
            nn.Linear(embed_dim, 512),
            nn.ReLU(),
            nn.Linear(512, input_dim)  # 输出的维度恢复到输入维度
        )

    def forward(self, x):
        x = x.to(torch.float32)
        embedding = self.encoder(x)
        return embedding


# Define a linear transformation to adjust `attendant` dimensions
class AttendantTransformer(nn.Module):
    def __init__(self, input_dim, output_dim):
        super(AttendantTransformer, self).__init__()
        self.linear = nn.Linear(input_dim, output_dim)

    def forward(self, x):
        return self.linear(x)


class IntraGraphAttention(nn.Module):
    def __init__(self, input_dim):
        super().__init__()
        self.input_dim = input_dim
        self.intra = GATConv(input_dim, 32, 2)

    def forward(self, data):
        input_feature, edge_index = data.x, data.edge_index
        input_feature = F.elu(input_feature)
        intra_rep = self.intra(input_feature, edge_index)
        return intra_rep


class InterGraphAttention(nn.Module):
    def __init__(self, input_dim):
        super().__init__()
        self.input_dim = input_dim
        self.inter = GATConv((input_dim, input_dim), 32, 2)

    def forward(self, h_data, t_data, b_graph):
        edge_index = b_graph.edge_index
        h_input = F.elu(h_data.x)
        t_input = F.elu(t_data.x)
        t_rep = self.inter((h_input, t_input), edge_index)
        h_rep = self.inter((t_input, h_input), edge_index[[1, 0]])
        return h_rep, t_rep


class Pool(nn.Module):
    def __init__(self, in_dim: int, ratio=0.5, conv_op=GraphConv, non_linearity=F.tanh):
        super().__init__()
        self.in_dim = in_dim
        self.ratio = ratio
        self.score_layer1 = conv_op(in_dim, 1)
        self.score_layer2 = nn.Linear(in_dim, 1)
        self.non_linearity = non_linearity
        self.reset_parameters()

    def reset_parameters(self):
        self.score_layer1.reset_parameters()
        self.score_layer2.reset_parameters()

    def forward(self, x, edge_index, edge_attr=None, batch=None):
        score1 = self.score_layer1(x, edge_index).squeeze()
        score2 = self.score_layer2(x).squeeze()
        score = torch.max(torch.cat((score1.unsqueeze(1), score2.unsqueeze(1)), dim=1), dim=1)[0]

        perm = topk(score, self.ratio, batch)

        x = x[perm] * self.non_linearity(score[perm]).view(-1, 1)
        edge_index, edge_attr = filter_adj(edge_index, edge_attr, perm,
                                           num_nodes=score.size(0))
        batch = batch[perm]

        return x, edge_index, batch


class MultiHeadSelfAttention(nn.Module):
    def __init__(self, dim_in, dim_k, dim_v, num_heads=8):
        super().__init__()
        assert dim_k % num_heads == 0 and dim_v % num_heads == 0, "dim_k and dim_v must be multiple of num_heads"
        self.dim_in = dim_in
        self.dim_k = dim_k
        self.dim_v = dim_v
        self.num_heads = num_heads
        self.linear_q = nn.Linear(dim_in, dim_k, bias=False)
        self.linear_k = nn.Linear(dim_in, dim_k, bias=False)
        self.linear_v = nn.Linear(dim_in, dim_v, bias=False)
        self._norm_fact = 1 / math.sqrt(dim_k // num_heads)
        self.norm = nn.LayerNorm(dim_v)
        self.reset_parameters()

    def reset_parameters(self):
        self.linear_q.reset_parameters()
        self.linear_k.reset_parameters()
        self.linear_v.reset_parameters()
        self.norm.reset_parameters()

    def forward(self, x):
        batch, n, dim_in = x.shape
        assert dim_in == self.dim_in

        nh = self.num_heads
        dk = self.dim_k // nh
        dv = self.dim_v // nh

        q = self.linear_q(x).reshape(batch, n, nh, dk).transpose(1, 2)
        k = self.linear_k(x).reshape(batch, n, nh, dk).transpose(1, 2)
        v = self.linear_v(x).reshape(batch, n, nh, dv).transpose(1, 2)

        dist = torch.matmul(q, k.transpose(2, 3)) * self._norm_fact
        dist = torch.softmax(dist, dim=-1)

        att = torch.matmul(dist, v)
        att = att.transpose(1, 2).reshape(batch, n, self.dim_v)
        att_n = self.norm(att)
        return att_n


class CoAttentionLayer(nn.Module):
    def __init__(self, n_features):
        super().__init__()
        self.n_features = n_features
        self.w_q = nn.Parameter(torch.zeros(n_features, n_features // 2))
        self.w_k = nn.Parameter(torch.zeros(n_features, n_features // 2))
        self.bias = nn.Parameter(torch.zeros(n_features // 2))
        self.a = nn.Parameter(torch.zeros(n_features // 2))

        nn.init.xavier_uniform_(self.w_q)
        nn.init.xavier_uniform_(self.w_k)
        nn.init.xavier_uniform_(self.bias.view(*self.bias.shape, -1))
        nn.init.xavier_uniform_(self.a.view(*self.a.shape, -1))

    def forward(self, receiver, attendant):
        keys = receiver @ self.w_k
        queries = attendant @ self.w_q
        values = receiver

        e_activations = queries.unsqueeze(-3) + keys.unsqueeze(-2) + self.bias
        e_scores = torch.tanh(e_activations) @ self.a
        attentions = e_scores
        return attentions


class RESCAL(nn.Module):
    def __init__(self, n_features):
        super().__init__()
        self.n_features = n_features

    def forward(self, heads, tails, rels, alpha_scores):
        rels = F.normalize(rels, dim=-1)
        heads = F.normalize(heads, dim=-1)
        tails = F.normalize(tails, dim=-1)

        rels = rels.view(-1, self.n_features, self.n_features)
        scores = heads @ rels @ tails.transpose(-2, -1)

        if alpha_scores is not None:
            scores = alpha_scores * scores
        scores = scores.sum(dim=(-2, -1))

        return scores

    def __repr__(self):
        return f"{self.__class__.__name__}({self.n_rels}, {self.rel_emb.weight.shape})"
